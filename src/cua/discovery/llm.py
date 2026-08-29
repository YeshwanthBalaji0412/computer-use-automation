"""The LLM boundary.

One narrow interface - `LLMClient.turn()` - with two implementations: the real Anthropic
client, and a `MockLLM` that replays a recorded transcript. Everything above this line is
identical either way, which is what makes `cua discover --mock` a genuine exercise of the
discovery loop rather than a separate code path that happens to produce an artifact.

Why a manual loop instead of the SDK's tool runner
--------------------------------------------------
The runner would drive the request/execute/loop cycle for us, and for a plain tool agent
that is the right default. Three things here argue against it:

* Every tool call has to pass through the policy engine, the control lease, and the
  recorder *before* it touches the surface. That sequence is the safety story, and it
  belongs somewhere a reviewer can read it in one place rather than behind callbacks.
* The stopping conditions are stateful across turns - a no-progress detector comparing
  observation fingerprints - which is loop logic, not tool logic.
* Owning the loop makes the model a swappable dependency. `MockLLM` needs no SDK, no
  network, and no key, so the entire discovery path is testable in CI for free.

The cost is roughly forty lines of loop we maintain ourselves. Worth it for the third
reason alone.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

DEFAULT_MODEL = "claude-opus-5"
MAX_TOKENS = 8_000


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    element_hint: str = Field(
        default="",
        description="'role|name' of the element this call resolved to, stamped by the "
        "agent after execution. Refs are per-observation, so a recorded transcript that "
        "trusted them would break the moment perception changed - which is exactly what "
        "happened when paragraph text started being perceived and every ref shifted.",
    )


class Turn(BaseModel):
    """One model response: some prose, and zero or more tool calls."""

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ToolResult(BaseModel):
    tool_use_id: str
    content: str
    is_error: bool = False


class LLMClient(Protocol):
    """The whole boundary. Two methods, one of them bookkeeping."""

    async def turn(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> Turn: ...

    @property
    def model_name(self) -> str: ...


class AnthropicClient:
    """The real thing."""

    def __init__(self, model: str | None = None) -> None:
        # Imported lazily so `--mock` runs with no SDK configuration and no key.
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic()
        self._model: str = model or os.environ.get("CUA_MODEL") or DEFAULT_MODEL

    @property
    def model_name(self) -> str:
        return self._model

    async def turn(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> Turn:
        # The SDK's overloads are TypedDict-based and do not infer from inline dict
        # literals for `thinking` / `output_config` / `tool_choice`. Importing those
        # param types just to satisfy the checker would couple this module to SDK
        # internals for no runtime benefit, so the boundary carries one narrow ignore.
        response = await self._client.messages.create(  # type: ignore[call-overload]
            model=self._model,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            # A browser has one cursor. Parallel tool calls would interleave clicks on
            # state that the earlier click already invalidated.
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            # The system prompt and tool schemas are byte-identical on every turn of a
            # thirty-step run, so they belong behind a cache breakpoint.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=messages,
        )

        text = "".join(b.text for b in response.content if b.type == "text")
        calls = [
            ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
            for b in response.content
            if b.type == "tool_use"
        ]
        return Turn(
            text=text,
            tool_calls=calls,
            stop_reason=response.stop_reason or "end_turn",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


class MockLLM:
    """Replays a recorded transcript.

    This is what makes the brief's "how to run without live services" answer real, and
    it is also the fast deterministic test of the recorder and compiler - the two
    components whose bugs are otherwise only visible after spending money.

    Recorded tool calls carry both a `ref` and the description of the element that ref
    pointed at. On replay the ref is used if it still names the same thing, and
    otherwise the element is found again by role and name. Refs are per-observation, so
    a fixture that trusted them blindly would rot the first time an element was added.
    """

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
            json.dumps(
                {"model": model, "turns": [t.model_dump() for t in turns]},
                indent=2,
            ),
            encoding="utf-8",
        )

    #: `  e19  link 'View' [contentFrame]  (row: Account=Savings | column: Action)`
    _LINE = re.compile(r"^\s+(e\d+)\s+([\w-]+)\s+'(.*?)'")
    _ROW = re.compile(r"\(row: (.*?) \| column: (.*?)\)")

    async def turn(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> Turn:
        if self._index >= len(self._turns):
            # Running off the end means the loop took a path the recording did not.
            # Ending the turn is the honest response; pretending to have more to say
            # would make the mock diverge silently from the real client.
            return Turn(text="(mock transcript exhausted)", stop_reason="end_turn")
        turn = self._turns[self._index]
        self._index += 1
        return self._rebind(turn, messages)

    def _rebind(self, turn: Turn, messages: list[dict[str, Any]]) -> Turn:
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
    def _element_index(cls, messages: list[dict[str, Any]]) -> dict[str, str]:
        """'role|name' -> ref, from the most recent rendered observation."""
        for message in reversed(messages):
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in reversed(content):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                index: dict[str, str] = {}
                for line in str(block.get("content", "")).splitlines():
                    match = cls._LINE.match(line)
                    if not match:
                        continue
                    ref, role, name = match.groups()
                    index.setdefault(f"{role}|{name}".lower(), ref)

                    # Extraction targets are hinted by column and a sibling cell rather
                    # than by their own text, so index those forms too.
                    row = cls._ROW.search(line)
                    if row:
                        pairs, column = row.groups()
                        for pair in pairs.split(", "):
                            if "=" in pair and not pair.startswith(f"{column}="):
                                index.setdefault(f"{role}|col:{column}|{pair}".lower(), ref)
                if index:
                    return index
        return {}
