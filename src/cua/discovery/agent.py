"""The observe -> decide -> act loop.

Every tool call travels the same path before it reaches the surface:

    policy check -> record intent -> act -> observe -> record outcome -> report back

That ordering is the safety story, and it is here rather than behind framework callbacks
so it can be read in one place. A blocked action is returned to the model as a tool
*result* ("blocked by policy: ..."), not raised - the model can then adapt, and it
physically cannot route around the check because the check happens on our side of the
boundary.

Stopping conditions, all four of them:

    finish              the model says it is done
    max_steps           a hard ceiling from policy
    wall clock          a timeout, also from policy
    no progress         the observation fingerprint has not moved for N acts

The last one is the interesting one. A model stuck in a loop will happily burn thirty
steps re-clicking a control that does nothing; the fingerprint ignores field values
precisely so that "the screen has not changed" is a reliable signal rather than one that
resets every time something is typed.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from cua.discovery.llm import LLMClient, ToolCall, Turn
from cua.discovery.prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_goal_message
from cua.discovery.recorder import Recorder
from cua.discovery.tools import (
    AssertStateArgs,
    ExtractArgs,
    FinishArgs,
    NavigateArgs,
    NoteOutcomeArgs,
    RequestHumanArgs,
    parse_args,
    tool_definitions,
)
from cua.evidence.logger import EventType, EvidenceLogger
from cua.policy.engine import PolicyEngine
from cua.schema.capability import ActionKind
from cua.schema.policy import DecisionKind
from cua.surface.base import Action, ActionType, ElementNode, Observation, Surface

#: Consecutive acts with an unchanged fingerprint before we call it a dead end.
NO_PROGRESS_LIMIT = 3


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


_ACTION_KINDS = {
    "navigate": ActionKind.NAVIGATE,
    "click": ActionKind.CLICK,
    "fill": ActionKind.FILL,
    "select": ActionKind.SELECT,
    "press": ActionKind.PRESS,
}

_SURFACE_ACTIONS = {
    "navigate": ActionType.NAVIGATE,
    "click": ActionType.CLICK,
    "fill": ActionType.FILL,
    "select": ActionType.SELECT,
    "press": ActionType.PRESS,
}


class StopReason:
    FINISHED = "finished"
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"
    NO_PROGRESS = "no_progress"
    HUMAN_REQUESTED = "human_requested"
    POLICY_ESCALATION = "policy_escalation"
    MODEL_ENDED = "model_ended"


@dataclass
class DiscoveryResult:
    stop_reason: str
    recorder: Recorder
    turns: list[Turn] = field(default_factory=list)
    summary: str = ""
    capability_id: str = ""
    description: str = ""
    escalation_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def succeeded(self) -> bool:
        return self.stop_reason == StopReason.FINISHED


class DiscoveryAgent:
    def __init__(
        self,
        *,
        surface: Surface,
        llm: LLMClient,
        policy: PolicyEngine,
        logger: EvidenceLogger,
        recorder: Recorder | None = None,
    ) -> None:
        self._surface = surface
        self._llm = llm
        self._policy = policy
        self._log = logger
        self._recorder = recorder or Recorder()
        self._observation: Observation | None = None
        self._fingerprints: list[str] = []

    async def run(self, *, goal: str, target: str, tenant: str) -> DiscoveryResult:
        started = time.monotonic()
        limits = self._policy.policy
        tools = tool_definitions()
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": build_goal_message(goal, target, tenant)}
        ]
        result = DiscoveryResult(stop_reason=StopReason.MODEL_ENDED, recorder=self._recorder)

        self._log.event(
            EventType.RUN_STARTED,
            goal=goal,
            target=target,
            tenant=tenant,
            model=self._llm.model_name,
            prompt_version=PROMPT_VERSION,
        )

        await self._navigate_to_entry(target)

        for step_no in range(1, limits.max_steps_per_run + 1):
            if (time.monotonic() - started) * 1000 > limits.max_run_duration_ms:
                result.stop_reason = StopReason.TIMEOUT
                break

            turn = await self._llm.turn(system=SYSTEM_PROMPT, tools=tools, messages=messages)
            result.turns.append(turn)
            result.input_tokens += turn.input_tokens
            result.output_tokens += turn.output_tokens
            self._log.event(EventType.MODEL_TURN, actor="model", step=step_no, text=turn.text[:400])

            if not turn.wants_tools:
                result.stop_reason = StopReason.MODEL_ENDED
                break

            messages.append({"role": "assistant", "content": self._assistant_content(turn)})

            results: list[dict[str, Any]] = []
            terminal: str | None = None

            for call in turn.tool_calls:
                if call.name == "finish":
                    args = parse_args("finish", call.arguments)
                    assert isinstance(args, FinishArgs)
                    result.summary = args.summary
                    result.capability_id = args.capability_id
                    result.description = args.description
                    terminal = StopReason.FINISHED
                    results.append(self._ok(call, "recorded; run complete"))
                    break

                if call.name == "request_human":
                    args_h = parse_args("request_human", call.arguments)
                    assert isinstance(args_h, RequestHumanArgs)
                    result.escalation_reason = args_h.reason
                    self._log.event(
                        EventType.ESCALATION_RAISED,
                        reason=args_h.reason,
                        tried=args_h.tried,
                    )
                    terminal = StopReason.HUMAN_REQUESTED
                    results.append(self._ok(call, "escalated to a human operator"))
                    break

                payload, escalated = await self._dispatch(call)
                results.append(payload)
                if escalated:
                    terminal = StopReason.POLICY_ESCALATION
                    break

            messages.append({"role": "user", "content": results})

            if terminal:
                result.stop_reason = terminal
                break

            if self._stalled():
                self._log.event(
                    EventType.ESCALATION_RAISED,
                    reason="no_progress",
                    detail=f"screen unchanged across {NO_PROGRESS_LIMIT} actions",
                )
                result.stop_reason = StopReason.NO_PROGRESS
                break
        else:
            result.stop_reason = StopReason.MAX_STEPS

        self._log.event(
            EventType.RUN_FINISHED,
            stop_reason=result.stop_reason,
            actions=len(self._recorder.actions),
            effective_actions=len(self._recorder.effective_actions),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        return result

    # ------------------------------------------------------------------ dispatch

    async def _dispatch(self, call: ToolCall) -> tuple[dict[str, Any], bool]:
        """Run one tool call. Returns (tool_result, escalated)."""
        try:
            if call.name == "observe":
                return self._ok(call, await self._do_observe()), False
            if call.name in _SURFACE_ACTIONS:
                return await self._do_action(call)
            if call.name == "extract":
                return self._ok(call, await self._do_extract(call)), False
            if call.name == "assert_state":
                return self._ok(call, self._do_assert(call)), False
            if call.name == "note_outcome":
                return self._ok(call, self._do_note(call)), False
        except Exception as exc:  # surfaced to the model, not fatal to the run
            self._log.error(f"tool {call.name} failed", detail=str(exc))
            return self._err(call, f"{type(exc).__name__}: {exc}"), False

        return self._err(call, f"unknown tool {call.name!r}"), False

    async def _do_observe(self) -> str:
        self._observation = await self._surface.observe()
        self._log.event(
            EventType.OBSERVED,
            url=self._observation.url,
            elements=len(self._observation.elements),
            fingerprint=self._observation.fingerprint,
        )
        return self._render(self._observation)

    async def _do_action(self, call: ToolCall) -> tuple[dict[str, Any], bool]:
        args = parse_args(call.name, call.arguments)
        kind = _ACTION_KINDS[call.name]
        before = self._observation or await self._surface.observe()

        element: ElementNode | None = None
        url: str | None = None
        if isinstance(args, NavigateArgs):
            url = args.url
        else:
            ref = getattr(args, "ref", "")
            element = before.by_ref(ref)
            if element is None:
                return (
                    self._err(
                        call,
                        f"no element {ref!r} in the current observation - call observe() "
                        f"again; refs expire whenever the screen changes",
                    ),
                    False,
                )

        decision = self._policy.check(
            kind,
            url=url,
            control_name=element.name if element else "",
            during_discovery=True,
        )
        self._log.event(
            EventType.POLICY_DECISION,
            tool=call.name,
            decision=decision.kind,
            rule=decision.rule,
            reason=decision.reason,
        )
        if decision.kind is DecisionKind.BLOCK:
            return self._err(call, f"blocked by policy: {decision.reason}"), False
        if decision.kind is DecisionKind.ESCALATE:
            self._log.event(
                EventType.ESCALATION_RAISED,
                reason="risky_step_needs_approval",
                detail=decision.reason,
                control=element.name if element else url,
            )
            return (
                self._ok(call, f"escalated for human approval: {decision.reason}"),
                True,
            )

        why = getattr(args, "why", "")
        if element is not None:
            # Stamped so this turn is replayable later: the ref will be meaningless, the
            # role and name will not.
            call.element_hint = f"{element.role}|{element.name}"

        # A value typed into a password field is registered with the redactor *before*
        # the first write, so it is masked in this event, in every later event, and in
        # the run manifest. Filtering at each call site instead would mean a secret is
        # masked only where somebody remembered to mask it.
        typed = getattr(args, "text", None)
        is_secret = bool(element and "secret" in element.states and typed)
        secret_ref = ""
        if is_secret and element and typed:
            secret_ref = f"corelink.{_slug(element.name) or 'secret'}"
            self._log.register_secret(secret_ref, typed)

        self._log.event(EventType.ACTION, tool=call.name, why=why, target=call.arguments)

        outcome = await self._surface.act(
            Action(
                type=_SURFACE_ACTIONS[call.name],
                ref=element.ref if element else None,
                url=url,
                text=getattr(args, "text", None),
                value=getattr(args, "value", None),
                key=getattr(args, "key", None),
            )
        )
        after = await self._surface.observe()
        self._observation = after
        self._fingerprints.append(after.fingerprint)

        self._recorder.record_action(
            action=kind,
            why=why,
            before=before,
            after=after,
            element=element,
            value=getattr(args, "text", None) or getattr(args, "value", None),
            is_parameter=bool(getattr(args, "is_parameter", False)),
            parameter_name=getattr(args, "parameter_name", ""),
            is_secret=is_secret,
            secret_ref=secret_ref,
            url=url,
            succeeded=outcome.ok,
        )

        if not outcome.ok:
            return self._err(call, f"action failed: {outcome.error}"), False
        return self._ok(call, f"done.\n\n{self._render(after)}"), False

    async def _do_extract(self, call: ToolCall) -> str:
        args = parse_args("extract", call.arguments)
        assert isinstance(args, ExtractArgs)
        observation = self._observation or await self._surface.observe()
        element = observation.by_ref(args.ref)
        if element is None:
            return f"no element {args.ref!r} in the current observation"
        call.element_hint = f"{element.role}|{element.name}"

        value = element.value or element.name
        if args.sensitive and value:
            # Declared regulated data. Registered before anything is written, so it is
            # masked in this event, in the manifest, and in the run report - the same
            # discipline as a typed credential.
            self._log.register_secret(args.output_name, value)

        self._recorder.record_action(
            action=ActionKind.EXTRACT,
            why=args.why,
            before=observation,
            after=observation,
            element=element,
            output_name=args.output_name,
            output_type=args.output_type,
            output_sensitive=args.sensitive,
        )
        self._log.event(
            EventType.ACTION,
            tool="extract",
            why=args.why,
            output=args.output_name,
            sensitive=args.sensitive,
        )
        # The value is echoed back so the model can reason about it, but it goes through
        # the redactor on its way into the evidence log.
        return f"extracted {args.output_name!r} = {value!r}"

    def _do_assert(self, call: ToolCall) -> str:
        args = parse_args("assert_state", call.arguments)
        assert isinstance(args, AssertStateArgs)
        observation = self._observation
        if observation is None:
            return "observe first"
        elements = [e for e in (observation.by_ref(r) for r in args.refs) if e is not None]
        self._recorder.record_checkpoint(args.describe, elements, observation)
        self._log.event(EventType.ASSERTION, describe=args.describe, elements=len(elements))
        return f"checkpoint recorded: {args.describe} ({len(elements)} elements)"

    def _do_note(self, call: ToolCall) -> str:
        args = parse_args("note_outcome", call.arguments)
        assert isinstance(args, NoteOutcomeArgs)
        observation = self._observation
        if observation is None:
            return "observe first"
        self._recorder.record_outcome(args.code, args.describe, args.text_pattern, observation)
        self._log.event(EventType.BUSINESS_OUTCOME, code=args.code, describe=args.describe)
        return f"outcome {args.code} recorded as a known business result"

    # ------------------------------------------------------------------ helpers

    async def _navigate_to_entry(self, target: str) -> None:
        decision = self._policy.check(ActionKind.NAVIGATE, url=target)
        if decision.kind is DecisionKind.BLOCK:
            raise PermissionError(f"entry point refused by policy: {decision.reason}")
        await self._surface.act(Action(type=ActionType.NAVIGATE, url=target))
        self._observation = await self._surface.observe()
        self._recorder.entry_url = self._observation.url

    def _stalled(self) -> bool:
        recent = self._fingerprints[-NO_PROGRESS_LIMIT:]
        return len(recent) == NO_PROGRESS_LIMIT and len(set(recent)) == 1

    @staticmethod
    def _render(observation: Observation) -> str:
        """What the model actually reads. Roles and names; never markup."""
        lines = [
            f"url: {observation.url}",
            f"title: {observation.title}",
            "",
        ]
        current: object = object()
        for node in observation.elements:
            if node.section != current:
                current = node.section
                lines.append(f"[{node.section or 'page'}]")
            row = ""
            if node.row_context and node.row_context.row_key:
                pairs = ", ".join(f"{k}={v}" for k, v in list(node.row_context.row_key.items())[:3])
                row = f"  (row: {pairs} | column: {node.row_context.column_header})"
            value = f" = {node.value!r}" if node.value else ""
            frame = f" [{'/'.join(node.frame_path)}]" if node.frame_path else ""
            lines.append(f"  {node.ref}  {node.role} {node.name!r}{value}{frame}{row}")
        if observation.truncated:
            lines.append("  ... (element list truncated)")
        return "\n".join(lines)

    @staticmethod
    def _assistant_content(turn: Turn) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        if turn.text:
            content.append({"type": "text", "text": turn.text})
        for call in turn.tool_calls:
            content.append(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            )
        return content

    @staticmethod
    def _ok(call: ToolCall, content: str) -> dict[str, Any]:
        return {"type": "tool_result", "tool_use_id": call.id, "content": content}

    @staticmethod
    def _err(call: ToolCall, content: str) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": content,
            "is_error": True,
        }
