"""The LLM boundary.

One interface - `LLMClient.turn()` - over provider-neutral message and tool types, with
two implementations: the live OpenAI client and a `MockLLM` that replays a recorded
transcript. Everything above this line is identical either way, which is what makes
`cua discover --mock` a genuine exercise of the discovery loop rather than a separate
code path that happens to produce an artifact.

On the neutral types
--------------------
`Message`, `ToolCall`, `ToolResult` and `ToolSpec` are ours, not a vendor's. Each client
translates them to its own wire format at the last possible moment.

That translation is worth its ~40 lines because the first version did not do it: the
agent built content blocks in one provider's shape and passed them straight through what
was nominally an abstraction. The method signature was provider-neutral; the payload was
not. Porting to a second provider is what revealed it - a seam you have never crossed is
a seam you have not tested.

Why a manual loop rather than an SDK's agent runner
---------------------------------------------------
* Every tool call has to pass the policy engine, the control lease, and the recorder
  *before* it touches the surface. That sequence is the safety story, and it belongs
  where a reviewer reads it in one place rather than behind framework callbacks.
* The stopping conditions are stateful across turns - a no-progress detector comparing
  observation fingerprints - which is loop logic, not tool logic.
* Owning the loop makes the model a swappable dependency. `MockLLM` needs no SDK, no
  network and no key, so the whole discovery path runs in CI for free.

On the provider
---------------
The brief leaves provider and model open. OpenAI here; the model is `CUA_MODEL`, and a
long agentic loop rewards the strongest tool-use model a key has access to. Nothing
outside this file knows which provider is in use - and replay never calls a model at all,
which an import-linter contract enforces.
"""

from __future__ import annotations

import json
import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import BaseModel, Field

DEFAULT_MODEL = "gpt-4o"
MAX_TOKENS = 8_000


# ---------------------------------------------------------------- neutral types


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    #: Results of tool calls, answering the assistant's previous turn.
    TOOL = "tool"


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    element_hint: str = Field(
        default="",
        description="Identity of the element this call resolved to - 'role|name', or a "
        "column/row form for extraction targets - stamped by the agent after execution. "
        "Refs are per-observation, so a recorded transcript that trusted them would "
        "break the moment perception changed, which is exactly what happened when "
        "paragraph text started being perceived and every ref shifted.",
    )


class ToolResult(BaseModel):
    tool_call_id: str
    content: str
    is_error: bool = False


class Message(BaseModel):
    role: Role
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)


class ToolSpec(BaseModel):
    """A tool the model may call. `arguments_schema` is plain JSON Schema."""

    name: str
    description: str
    arguments_schema: dict[str, Any]


class Turn(BaseModel):
    """One model response: some prose, and zero or more tool calls."""

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: str = "stop"
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient(Protocol):
    """The whole boundary."""

    async def turn(
        self, *, system: str, tools: list[ToolSpec], messages: list[Message]
    ) -> Turn: ...

    @property
    def model_name(self) -> str: ...


# ---------------------------------------------------------------- live client


class OpenAIClient:
    """The live model."""

    def __init__(self, model: str | None = None) -> None:
        # Imported lazily so `--mock` runs with no SDK configuration and no key.
        from dotenv import load_dotenv
        from openai import AsyncOpenAI

        # Loaded here rather than only in the CLI. The credential is needed at exactly
        # one place - this constructor - and putting the load anywhere further out means
        # every new entry point has to remember: `scripts/record_flow_b.py` calls
        # `run_discover` directly, skipped the CLI, and failed with "Missing credentials"
        # against a .env that was sitting right there. `override=False` so a real
        # environment variable always wins over the file.
        load_dotenv(Path.cwd() / ".env", override=False)

        self._client = AsyncOpenAI()
        self._model: str = model or os.environ.get("CUA_MODEL") or DEFAULT_MODEL

    @property
    def model_name(self) -> str:
        return self._model

    async def turn(self, *, system: str, tools: list[ToolSpec], messages: list[Message]) -> Turn:
        response = await self._client.chat.completions.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            # A browser has one cursor. Parallel tool calls would interleave clicks on
            # state that the earlier click already invalidated.
            parallel_tool_calls=False,
            tools=cast("Any", [_tool_wire(t) for t in tools]),
            messages=cast(
                "Any", [{"role": "system", "content": system}, *_messages_wire(messages)]
            ),
        )

        choice = response.choices[0]
        calls: list[ToolCall] = []
        for call in choice.message.tool_calls or []:
            # The SDK's tool-call union also covers custom tools, which carry no
            # `.function`. Narrowing on the discriminator rather than reaching for the
            # attribute keeps this honest if that union grows again.
            if call.type != "function":
                continue
            calls.append(
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    # Arguments arrive as a JSON *string*. Always parse rather than
                    # string-matching: escaping differs between models and versions.
                    arguments=_safe_json(call.function.arguments),
                )
            )

        usage = response.usage
        return Turn(
            text=choice.message.content or "",
            tool_calls=calls,
            stop_reason=choice.finish_reason or "stop",
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
        )


def _tool_wire(tool: ToolSpec) -> dict[str, Any]:
    """Neutral spec -> OpenAI's function-tool shape.

    `strict` is deliberately not set: it requires every property to appear in `required`,
    and several of these tools have genuinely optional arguments with defaults. Arguments
    are validated against the same Pydantic model that generated the schema when they
    come back, so a malformed call is still rejected - one layer later, with a message
    the model can act on.
    """
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.arguments_schema,
        },
    }


def _messages_wire(messages: list[Message]) -> list[dict[str, Any]]:
    """Neutral messages -> OpenAI's chat format.

    One neutral TOOL message can carry several results; OpenAI wants one message per
    result, each keyed by `tool_call_id`.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role is Role.TOOL:
            out.extend(
                {
                    "role": "tool",
                    "tool_call_id": result.tool_call_id,
                    "content": result.content,
                }
                for result in message.tool_results
            )
        elif message.role is Role.ASSISTANT:
            entry: dict[str, Any] = {"role": "assistant", "content": message.text or None}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in message.tool_calls
                ]
            out.append(entry)
        else:
            out.append({"role": "user", "content": message.text})
    return out


def _safe_json(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------- recorded client


class MockLLM:
    """Replays a recorded transcript.

    This is what makes the brief's "how to run without live services" answer real, and it
    is also the fast deterministic test of the recorder and compiler - the two components
    whose bugs are otherwise only visible after spending money.
    """

    #: `  e19  link 'View' [contentFrame]  (row: Account=Savings | column: Action)`
    _LINE = re.compile(r"^\s+(e\d+)\s+([\w-]+)\s+'(.*?)'")
    _ROW = re.compile(r"\(row: (.*?) \| column: (.*?)\)")

    def __init__(self, turns: list[Turn], *, model: str = "mock") -> None:
        self._turns = turns
        self._index = 0
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    @classmethod
    def from_fixture(cls, path: Path) -> MockLLM:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls([Turn.model_validate(t) for t in raw["turns"]], model=raw.get("model", "mock"))

    @classmethod
    def write_fixture(cls, path: Path, turns: list[Turn], *, model: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"model": model, "turns": [t.model_dump() for t in turns]}, indent=2),
            encoding="utf-8",
        )

    async def turn(self, *, system: str, tools: list[ToolSpec], messages: list[Message]) -> Turn:
        if self._index >= len(self._turns):
            # Running off the end means the loop took a path the recording did not.
            # Ending the turn is the honest response; pretending to have more to say
            # would make the mock diverge silently from a live client.
            return Turn(text="(mock transcript exhausted)", stop_reason="stop")
        turn = self._turns[self._index]
        self._index += 1
        return self._rebind(turn, messages)

    def _rebind(self, turn: Turn, messages: list[Message]) -> Turn:
        """Re-resolve recorded refs against the current screen.

        A ref is only meaningful inside the observation that produced it. Replaying one
        verbatim works right up until perception changes - adding a role, reordering an
        element - at which point the transcript silently targets the wrong thing and
        produces a subtly wrong artifact rather than an obvious failure. Re-resolving by
        role and accessible name is the same identity the locator ladder uses, so the
        fixture stays valid for as long as the screen still says the same things.
        """
        index = self._element_index(messages)
        if not index:
            return turn

        rebound: list[ToolCall] = []
        for call in turn.tool_calls:
            hint = call.element_hint
            if hint and "|" in hint and "ref" in call.arguments:
                found = index.get(hint.lower())
                if found and found != call.arguments["ref"]:
                    call = call.model_copy(update={"arguments": {**call.arguments, "ref": found}})
            rebound.append(call)
        return turn.model_copy(update={"tool_calls": rebound})

    @classmethod
    def _element_index(cls, messages: list[Message]) -> dict[str, str]:
        """Element identity -> ref, from the most recent rendered observation."""
        for message in reversed(messages):
            if message.role is not Role.TOOL:
                continue
            for result in reversed(message.tool_results):
                index: dict[str, str] = {}
                for line in result.content.splitlines():
                    match = cls._LINE.match(line)
                    if not match:
                        continue
                    ref, role, name = match.groups()
                    index.setdefault(f"{role}|{name}".lower(), ref)

                    # Extraction targets are hinted by column and a sibling cell rather
                    # than by their own text - a cell's accessible name *is* the value -
                    # so index those forms too.
                    row = cls._ROW.search(line)
                    if row:
                        pairs, column = row.groups()
                        for pair in pairs.split(", "):
                            if "=" in pair and not pair.startswith(f"{column}="):
                                index.setdefault(f"{role}|col:{column}|{pair}".lower(), ref)
                if index:
                    return index
        return {}
