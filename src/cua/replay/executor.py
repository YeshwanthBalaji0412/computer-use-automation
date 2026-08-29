"""Deterministic replay.

**No LLM.** Not by convention - an import-linter contract forbids this package from
importing `anthropic`, and a test runs a full replay in a subprocess and asserts the
module was never even loaded.

Per-step sequence:

    bind params -> pre_assert -> policy check -> resolve locator -> act
                -> wait -> observe -> classify -> post_assert

Two orderings in that list are load-bearing:

* **Classify before post_assert.** An exceptional state must be recognised as *itself*
  rather than as "the checkpoint failed". Get this backwards and "no such member"
  arrives at the caller as a crash, which the brief calls the most common design mistake
  in this problem.
* **Policy check before resolve.** A refused action must not even locate its target.
  Nothing about a blocked step should touch the page.

How determinism is achieved, since that is the question the write-up has to answer:

1. Locators are re-derived from role, accessible name and data context at run time -
   never replayed from a recorded selector (see `cua.locator`).
2. Resolution requires a *unique* match. Ambiguity is an error, never `.first`.
3. Every wait is a condition with a timeout. There is no `sleep()` in this package.
4. Every step is guarded by a precondition and verified by a postcondition, so a run
   cannot silently drift off the recorded path.
5. Recovery attempts are capped and counted.
6. Outputs are parsed to declared types, so a rendering difference is not a value
   difference.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from cua.control.intervention import Intervention, InterventionStore, suggest
from cua.control.session import Disposition, SessionController
from cua.evidence.logger import EventType, EvidenceLogger
from cua.locator.generate import bind
from cua.locator.match import resolve as match_resolve
from cua.policy.engine import PolicyEngine
from cua.replay.assertions import evaluate_all, screen_text
from cua.replay.classifier import Classification, StateClass, StateClassifier
from cua.replay.extract import ExtractionError, jsonable, parse
from cua.schema.capability import (
    ActionKind,
    ApprovalStatus,
    Capability,
    RecoveryActionKind,
    Step,
)
from cua.schema.locator import Locator, Resolution, ResolveOutcome
from cua.schema.policy import DecisionKind
from cua.schema.result import (
    Blocked,
    BusinessOutcome,
    ErrorClass,
    Escalated,
    EscalationReason,
    EvidenceRef,
    Failed,
    InterventionRef,
    Outcome,
    PolicyViolation,
    RecoveryRecord,
    ReplayError,
    ReplayResult,
    StepStatus,
    StepTrace,
    Success,
)
from cua.surface.base import Action, ActionType, Observation, Surface

_SURFACE_ACTIONS = {
    ActionKind.NAVIGATE: ActionType.NAVIGATE,
    ActionKind.CLICK: ActionType.CLICK,
    ActionKind.FILL: ActionType.FILL,
    ActionKind.SELECT: ActionType.SELECT,
    ActionKind.PRESS: ActionType.PRESS,
}


class _RetryStep:
    """Sentinel: a human handed control back and the step should be attempted again."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<RETRY_STEP>"


RETRY_STEP = _RetryStep()

#: How many times a step may be resumed into before we stop and raise a fresh
#: intervention. An operator who keeps hitting the same wall needs a different problem
#: statement, not another attempt.
MAX_STEP_REPEATS = 3


class InputValidationError(ValueError):
    """The caller's arguments do not satisfy the capability's declared contract."""


@dataclass
class _Ctx:
    """Mutable state for one replay."""

    values: dict[str, str]
    traces: list[StepTrace] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    observation: Observation | None = None
    drift: bool = False
    #: How many times this run has come back from a human. Bounds the case where an
    #: operator keeps resuming into a condition they have not actually cleared.
    handoffs: int = 0


class ReplayExecutor:
    def __init__(
        self,
        *,
        surface: Surface,
        capability: Capability,
        policy: PolicyEngine,
        logger: EvidenceLogger,
        base_url: str,
        tenant: str | None = None,
        secrets: dict[str, str] | None = None,
        controller: SessionController | None = None,
        interventions: InterventionStore | None = None,
        operator_base_url: str = "",
        goal: str = "",
    ) -> None:
        self._surface = surface
        self._cap = capability
        self._policy = policy
        self._log = logger
        self._base_url = base_url.rstrip("/")
        self._tenant = tenant
        self._secrets = secrets or {}
        self._classifier = StateClassifier(capability)
        #: Present only when a human can actually be reached. Without them, an
        #: escalation is still reported honestly - it just terminates the run instead of
        #: parking it. Unattended and attended execution share one code path.
        self._controller = controller
        self._interventions = interventions
        self._operator_base_url = operator_base_url.rstrip("/")
        self._goal = goal

    # ------------------------------------------------------------------ entry

    async def run(self, inputs: dict[str, str]) -> ReplayResult:
        started = time.monotonic()
        self._log.event(
            EventType.RUN_STARTED,
            capability=self._cap.qualified_name,
            tenant=self._tenant,
            inputs=inputs,
        )

        try:
            values = self._validate_inputs(inputs)
        except InputValidationError as exc:
            # Fails before the browser is even used. A malformed member id should cost
            # nothing, and the caller gets a contract violation rather than a UI error.
            return self._fail(
                started,
                ErrorClass.INVALID_INPUT,
                expected="inputs matching the declared contract",
                observed=str(exc),
            )

        for param in self._cap.inputs:
            if param.sensitivity in ("pii", "secret") and param.name in values:
                self._log.register_secret(param.name, values[param.name])

        ctx = _Ctx(values=values)

        try:
            outcome = await self._run_steps(ctx)
        except Exception as exc:  # anything unexpected becomes evidence, not a traceback
            self._log.error("replay crashed", detail=f"{type(exc).__name__}: {exc}")
            return self._fail(
                started,
                ErrorClass.SURFACE_ERROR,
                expected="the recorded flow to run",
                observed=f"{type(exc).__name__}: {exc}",
                ctx=ctx,
            )

        if outcome is not None:
            return self._finish(outcome, started, ctx)

        verdict = await self._check_success(ctx)
        if verdict is not None:
            return self._finish(verdict, started, ctx)

        return self._finish(
            Success(
                capability=self._cap.qualified_name,
                tenant=self._tenant,
                outputs=ctx.outputs,
            ),
            started,
            ctx,
        )

    # ------------------------------------------------------------------ steps

    async def _enter(self, ctx: _Ctx) -> ReplayResult | None:
        """Navigate to the capability's declared entry point.

        Discovery navigates here *before* its loop starts, so the move is not one of the
        recorded steps - it is a property of the capability. Replay has to perform it
        explicitly, or step 1 runs against whatever page the browser happened to open on.
        The URL is a canonical pattern, so this is also where `{{base_url}}` becomes a
        particular tenant's deployment.
        """
        url = self._expand(self._cap.target.entry_url_pattern, ctx)
        if not url:
            ctx.observation = await self._surface.observe()
            return None

        decision = self._policy.check(ActionKind.NAVIGATE, url=url)
        if decision.kind is not DecisionKind.ALLOW:
            return Blocked(
                capability=self._cap.qualified_name,
                tenant=self._tenant,
                policy=PolicyViolation(
                    rule=decision.rule,
                    attempted=f"navigate to the entry point {url}",
                    required_approval=decision.required_approval,
                ),
            )

        await self._surface.act(Action(type=ActionType.NAVIGATE, url=url))
        self._log.event(EventType.ACTION, intent="navigate to the capability entry point")
        ctx.observation = await self._surface.observe()
        return None

    async def _run_steps(self, ctx: _Ctx) -> ReplayResult | None:
        entry = await self._enter(ctx)
        if entry is not None:
            return entry
        self._check_drift(ctx)

        ok, results = evaluate_all(
            self._cap.preconditions,
            ctx.observation,  # type: ignore[arg-type]
            self._output_locators(),
        )
        if not ok:
            failed = next(r for r in results if not r.passed)
            return self._error(
                ErrorClass.PRECONDITION_FAILED, None, failed.describe, failed.observed, ctx
            )

        index = 0
        repeats = 0
        while index < len(self._cap.steps):
            step = self._cap.steps[index]
            began = time.monotonic()
            self._log.event(EventType.STEP_STARTED, step_id=step.id, intent=step.intent)

            result = await self._run_step(step, ctx)

            if result is RETRY_STEP:
                # A human cleared the obstacle and handed control back. Repeat the step,
                # but boundedly: an operator who keeps resuming into the same wall should
                # get a fresh intervention rather than an infinite loop.
                repeats += 1
                if repeats > MAX_STEP_REPEATS:
                    exhausted = await self._escalate(
                        EscalationReason.RECOVERY_EXHAUSTED,
                        f"step {step.id} was resumed {repeats} times without progressing",
                        step,
                        ctx,
                        allow_handoff=False,
                    )
                    # allow_handoff=False cannot return a retry signal.
                    assert not isinstance(exhausted, _RetryStep)
                    return exhausted
                continue
            if result is not None:
                return result  # type: ignore[return-value]

            self._log.event(
                EventType.STEP_FINISHED,
                step_id=step.id,
                duration_ms=int((time.monotonic() - began) * 1000),
            )
            index += 1
            repeats = 0
        return None

    async def _run_step(self, step: Step, ctx: _Ctx) -> ReplayResult | _RetryStep | None:
        trace = StepTrace(
            step_id=step.id, intent=step.intent, action=str(step.action), status=StepStatus.OK
        )
        ctx.traces.append(trace)

        assert ctx.observation is not None
        ok, results = evaluate_all(step.pre_assert, ctx.observation, self._output_locators())
        if not ok:
            trace.status = StepStatus.FAILED
            failed = next(r for r in results if not r.passed)
            return self._error(
                ErrorClass.PRECONDITION_FAILED, step, failed.describe, failed.observed, ctx
            )

        decision = self._policy.check(
            step.action,
            url=self._expand(step.url, ctx) if step.url else None,
            # From the artifact, not the live page: policy is checked before the locator
            # is resolved, so nothing about a refused step ever touches the application.
            control_name=step.control_name,
            declared_risk=step.risk,
            capability_approved=self._cap.status is ApprovalStatus.APPROVED,
        )
        self._log.event(
            EventType.POLICY_DECISION,
            step_id=step.id,
            decision=decision.kind,
            rule=decision.rule,
        )
        if decision.kind is not DecisionKind.ALLOW:
            trace.status = StepStatus.FAILED
            return Blocked(
                capability=self._cap.qualified_name,
                tenant=self._tenant,
                steps=ctx.traces,
                policy=PolicyViolation(
                    rule=decision.rule,
                    attempted=f"{step.action} - {step.intent}",
                    required_approval=decision.required_approval,
                ),
            )

        if step.action is ActionKind.EXTRACT:
            return await self._extract(step, ctx, trace)
        if step.action is ActionKind.ASSERT:
            return None

        return await self._act(step, ctx, trace)

    async def _act(
        self, step: Step, ctx: _Ctx, trace: StepTrace
    ) -> ReplayResult | _RetryStep | None:
        assert ctx.observation is not None
        ref: str | None = None

        if step.target is not None:
            resolution = await self._resolve_with_retry(step.target, step, ctx, trace)
            trace.winning_tier = resolution.winning_tier
            trace.tiers_tried = resolution.tiers_tried
            trace.locator_degraded = resolution.degraded

            if resolution.outcome is not ResolveOutcome.RESOLVED:
                trace.status = StepStatus.FAILED
                cls = (
                    ErrorClass.LOCATOR_AMBIGUOUS
                    if resolution.outcome is ResolveOutcome.AMBIGUOUS
                    else ErrorClass.LOCATOR_UNRESOLVED
                )
                return self._error(
                    cls, step, step.target.describe, resolution.detail, ctx, step.target
                )
            ref = resolution.ref

            if resolution.degraded:
                # The step worked, but a better-ranked strategy stopped working. That is
                # the earliest warning available that this tenant's UI has moved.
                ctx.drift = True
                self._log.event(
                    EventType.LOCATOR_DEGRADED,
                    step_id=step.id,
                    winning_tier=int(resolution.winning_tier or 0),
                    tiers_tried=[int(t) for t in resolution.tiers_tried],
                )

        outcome = await self._surface.act(
            Action(
                type=_SURFACE_ACTIONS[step.action],
                ref=ref,
                url=self._expand(step.url, ctx),
                text=self._value_for(step, ctx),
                value=self._value_for(step, ctx),
            )
        )
        self._log.event(EventType.ACTION, step_id=step.id, intent=step.intent, ok=outcome.ok)

        ctx.observation = await self._surface.observe()

        classification = self._classifier.classify(ctx.observation)
        resolved = await self._handle(classification, step, ctx, trace)
        if resolved is not None:
            return resolved

        if not outcome.ok:
            trace.status = StepStatus.FAILED
            return self._error(
                ErrorClass.SURFACE_ERROR, step, step.intent, outcome.error or "", ctx
            )

        ok, results = evaluate_all(step.post_assert, ctx.observation, self._output_locators())
        if not ok:
            trace.status = StepStatus.FAILED
            failed = next(r for r in results if not r.passed)
            return self._error(
                ErrorClass.CHECKPOINT_FAILED, step, failed.describe, failed.observed, ctx
            )
        return None

    async def _handle(
        self, classification: Classification, step: Step, ctx: _Ctx, trace: StepTrace
    ) -> ReplayResult | _RetryStep | None:
        """Act on the classifier's verdict. Terminal result, retry signal, or None."""
        if classification.state is StateClass.CLEAN:
            return None

        if classification.state is StateClass.BUSINESS_OUTCOME:
            outcome = classification.outcome
            assert outcome is not None
            self._log.event(EventType.BUSINESS_OUTCOME, step_id=step.id, code=outcome.code)
            return BusinessOutcome(
                capability=self._cap.qualified_name,
                tenant=self._tenant,
                steps=ctx.traces,
                outcome=Outcome(
                    code=outcome.code, message=outcome.message, data=dict(outcome.returns)
                ),
            )

        if classification.state is StateClass.RECOVERABLE:
            recovery = classification.recovery
            assert recovery is not None
            attempt = self._classifier.attempts_for(recovery.id)
            applied = await self._recover(recovery, ctx)
            trace.status = StepStatus.RECOVERED
            trace.recoveries.append(
                RecoveryRecord(
                    recovery_id=recovery.id,
                    attempt=attempt,
                    succeeded=applied,
                    detail=classification.detail,
                )
            )
            self._log.event(
                EventType.RECOVERY,
                step_id=step.id,
                recovery=recovery.id,
                attempt=attempt,
                succeeded=applied,
            )
            return None

        reason = classification.escalation_reason or EscalationReason.AMBIGUOUS_STATE
        return await self._escalate(reason, classification.detail, step, ctx)

    async def _recover(self, recovery: Any, ctx: _Ctx) -> bool:
        """Apply a recovery and report **honestly** whether it did anything.

        Returning True unconditionally would put "succeeded: true" in the evidence for a
        dismiss that never found its button - which is worse than the failure itself,
        because it hides it.
        """
        action = recovery.action
        applied = False

        if action.kind is RecoveryActionKind.DISMISS and action.locator is not None:
            resolution = await self._resolve(action.locator, ctx)
            if resolution.ref:
                outcome = await self._surface.act(Action(type=ActionType.CLICK, ref=resolution.ref))
                applied = outcome.ok
        elif action.kind is RecoveryActionKind.RELOAD and ctx.observation:
            outcome = await self._surface.act(
                Action(type=ActionType.NAVIGATE, url=ctx.observation.url)
            )
            applied = outcome.ok
        else:
            # WAIT_AND_RETRY needs no action: re-observing is the retry, and the surface
            # already waits on a load-state condition.
            applied = True

        ctx.observation = await self._surface.observe()
        return applied

    async def _extract(self, step: Step, ctx: _Ctx, trace: StepTrace) -> ReplayResult | None:
        assert ctx.observation is not None
        field_def = next((o for o in self._cap.outputs if o.name == step.output), None)
        locator = self._cap.effective_locator(step)
        if field_def is None or locator is None:
            trace.status = StepStatus.FAILED
            return self._error(
                ErrorClass.OUTPUT_EXTRACTION_FAILED, step, step.output or "", "no locator", ctx
            )

        resolution = await self._resolve_with_retry(locator, step, ctx, trace)
        trace.winning_tier = resolution.winning_tier
        trace.tiers_tried = resolution.tiers_tried
        trace.locator_degraded = resolution.degraded

        assert ctx.observation is not None
        node = ctx.observation.by_ref(resolution.ref or "")
        if node is None:
            trace.status = StepStatus.FAILED
            return self._error(
                ErrorClass.OUTPUT_EXTRACTION_FAILED,
                step,
                f"a value for {field_def.name!r} at {locator.describe}",
                resolution.detail,
                ctx,
                locator,
            )

        raw = node.value or node.name
        try:
            value = parse(raw, field_def.parse, field=field_def.name)
        except ExtractionError as exc:
            trace.status = StepStatus.FAILED
            return self._error(
                ErrorClass.OUTPUT_EXTRACTION_FAILED,
                step,
                f"{field_def.name} parseable as {field_def.parse}",
                str(exc),
                ctx,
            )

        if field_def.sensitivity in ("pii", "secret"):
            self._log.register_secret(field_def.name, str(raw))
        ctx.outputs[field_def.name] = jsonable(value)
        self._log.event(EventType.ACTION, step_id=step.id, extracted=field_def.name)
        return None

    # ------------------------------------------------------------------ finishing

    async def _check_success(self, ctx: _Ctx) -> ReplayResult | None:
        if self._cap.success_condition is None:
            return None
        assert ctx.observation is not None
        ok, results = evaluate_all(
            [self._cap.success_condition], ctx.observation, self._output_locators()
        )
        if ok:
            return None
        failed = results[0]
        return self._error(
            ErrorClass.CHECKPOINT_FAILED,
            None,
            failed.describe or "the declared success condition",
            failed.observed or self._summarise(ctx.observation),
            ctx,
        )

    async def _escalate(
        self,
        reason: EscalationReason,
        detail: str,
        step: Step | None,
        ctx: _Ctx,
        *,
        allow_handoff: bool = True,
    ) -> ReplayResult | _RetryStep:
        """Raise an intervention, and if a human can be reached, hand over and wait.

        With no controller wired in - an unattended run - this reports the escalation
        honestly and stops. Attended and unattended execution share one path; the only
        difference is whether there is anybody to cede to.
        """
        self._log.event(
            EventType.ESCALATION_RAISED,
            step_id=step.id if step else None,
            reason=str(reason),
            detail=detail,
        )

        intervention = await self._raise_intervention(reason, detail, step, ctx)

        if not (allow_handoff and self._controller is not None):
            return self._escalated_result(reason, step, ctx, intervention)

        if self._interventions is not None and intervention is not None:
            print(f"\n  intervention raised: {intervention.operator_url}", flush=True)

        self._log.event(
            EventType.CONTROL_TRANSFERRED,
            actor="system",
            step_id=step.id if step else None,
            to="human",
            epoch=self._controller.epoch + 1,
        )

        # Parks here. The browser is untouched; the operator console is served by this
        # same event loop, which is why the async Playwright API is not optional.
        handoff = await self._controller.cede()

        for action in handoff.actions:
            self._log.event(
                EventType.HUMAN_ACTION,
                actor="human",
                step_id=step.id if step else None,
                detail=action.describe(),
            )
        self._log.event(
            EventType.CONTROL_TRANSFERRED,
            actor="system",
            to="automation",
            epoch=self._controller.epoch,
            disposition=str(handoff.disposition),
            human_actions=len(handoff.actions),
        )
        if self._interventions is not None and intervention is not None:
            self._interventions.resolve(
                intervention.id,
                handoff.disposition,
                note=handoff.note,
                actions=handoff.actions,
            )

        return await self._reorient(handoff, step, ctx, reason, intervention)

    async def _reorient(
        self,
        handoff: Any,
        step: Step | None,
        ctx: _Ctx,
        reason: EscalationReason,
        intervention: Intervention | None,
    ) -> ReplayResult | _RetryStep:
        """Work out where the session actually is, now that a human has been in it.

        Resuming is a re-orientation problem, not a resume-from-a-line-number problem.
        The operator may have done more than asked, less than asked, or something else
        entirely, and their stated disposition is a *hint* - the assertions are the
        evidence. Checking the screen first is what stops a mistaken "I did that" from
        silently skipping a step that never happened.
        """
        if handoff.disposition is Disposition.ABORT:
            return self._escalated_result(reason, step, ctx, intervention, handoff=handoff)

        ctx.observation = await self._surface.observe()
        assert ctx.observation is not None
        ctx.handoffs += 1

        # Re-classify before anything else. The operator's disposition is a claim; the
        # screen is the evidence. Without this a "yes I handled it" while an undeclared
        # dialog is still up would be accepted, and the run would report success with the
        # application still waiting on an answer - which at a bank is the wrong direction
        # to be wrong in.
        still = self._classifier.classify(ctx.observation)
        if still.state is StateClass.ESCALATE:
            if ctx.handoffs >= MAX_STEP_REPEATS:
                return self._escalated_result(
                    still.escalation_reason or reason, step, ctx, intervention, handoff=handoff
                )
            return await self._escalate(
                still.escalation_reason or reason,
                f"control was handed back as {handoff.disposition}, but the condition "
                f"is still present: {still.detail}",
                step,
                ctx,
            )
        if still.state is StateClass.BUSINESS_OUTCOME and still.outcome is not None:
            return BusinessOutcome(
                capability=self._cap.qualified_name,
                tenant=self._tenant,
                steps=ctx.traces,
                outcome=Outcome(
                    code=still.outcome.code,
                    message=still.outcome.message,
                    data=dict(still.outcome.returns),
                ),
            )

        if self._cap.success_condition is not None:
            done, _ = evaluate_all(
                [self._cap.success_condition], ctx.observation, self._output_locators()
            )
            if done:
                # The operator finished the whole flow by hand. Outputs still have to be
                # read, and the result records who actually completed it.
                await self._extract_all(ctx)
                return Success(
                    capability=self._cap.qualified_name,
                    tenant=self._tenant,
                    steps=ctx.traces,
                    outputs=ctx.outputs,
                    completed_by="human",
                    drift_suspected=ctx.drift,
                )

        if step is not None:
            advanced, _ = evaluate_all(step.post_assert, ctx.observation, self._output_locators())
            if advanced and step.post_assert:
                self._log.event(
                    EventType.STEP_FINISHED,
                    step_id=step.id,
                    completed_by="human",
                    note="post-condition satisfied after handoff",
                )
                return None  # type: ignore[return-value]

            ready, _ = evaluate_all(step.pre_assert, ctx.observation, self._output_locators())
            if ready or not step.pre_assert:
                return RETRY_STEP

        # The operator left the session somewhere neither expected nor recognisable.
        # Guessing here is exactly what must not happen, so ask again with fresh context.
        return await self._escalate(
            EscalationReason.AMBIGUOUS_STATE,
            "control was handed back but the session is in a state this capability "
            "does not recognise",
            step,
            ctx,
            allow_handoff=False,
        )

    async def _raise_intervention(
        self, reason: EscalationReason, detail: str, step: Step | None, ctx: _Ctx
    ) -> Intervention | None:
        if self._interventions is None:
            if ctx.observation is not None:
                self._register_visible_secrets(ctx)
                self._log.write_failure_context(
                    aria_snapshot=ctx.observation.aria_yaml,
                    observations=[ctx.observation.model_dump(mode="json")],
                    summary=f"escalated at {step.id if step else 'success check'}: {detail}",
                )
            return None

        shot: str | None = None
        if ctx.observation is not None:
            self._register_visible_secrets(ctx)
            self._log.write_failure_context(
                aria_snapshot=ctx.observation.aria_yaml,
                observations=[ctx.observation.model_dump(mode="json")],
                summary=f"escalated at {step.id if step else 'success check'}: {detail}",
            )
            shot = await self._capture_screenshot(step)

        item = Intervention(
            id=f"int_{self._log.run_id}_{len(self._interventions.all_items()) + 1}",
            run_id=self._log.run_id,
            capability=self._cap.qualified_name,
            goal=self._goal,
            tenant=self._tenant,
            step_id=step.id if step else None,
            step_intent=step.intent if step else "verify the capability reached its goal",
            reason=reason,
            explain=detail,
            attempted=[f"{t.step_id}: {t.intent} ({t.status})" for t in ctx.traces[-4:]],
            url=ctx.observation.url if ctx.observation else "",
            screenshot_path=shot,
            aria_snapshot=ctx.observation.aria_yaml if ctx.observation else "",
            suggested_actions=suggest(reason),
        )
        item.operator_url = f"{self._operator_base_url}/interventions/{item.id}"
        return self._interventions.raise_(item)

    async def _capture_screenshot(self, step: Step | None) -> str | None:
        """Masked before the PNG is encoded, so raw pixels of regulated data never land
        on disk - even in an intervention an operator is about to look at."""
        try:
            path = self._log.step_screenshot_path(step.id if step else "escalation", "pre")
            await self._surface.screenshot(path, mask=self._sensitive_locators())
            return str(path)
        except Exception:  # a screenshot is evidence, never a reason to fail a run
            return None

    def _register_visible_secrets(self, ctx: _Ctx) -> None:
        """Mask declared-sensitive values as soon as they are *on screen*.

        Registration otherwise happens at extraction, which is too late for a run that
        escalates or fails earlier: the failure context captures the whole screen, and a
        balance is visible on it well before the step that reads it. Currency patterns
        are deliberately not masked by the backstop - masking every amount would make
        evidence unreadable - so the declared locator is the only thing that knows this
        particular number is regulated.
        """
        if ctx.observation is None:
            return
        for output in self._cap.outputs:
            if output.sensitivity not in ("pii", "secret") or output.locator is None:
                continue
            resolution = match_resolve(bind(output.locator, ctx.values), ctx.observation)
            node = ctx.observation.by_ref(resolution.ref or "")
            if node is not None:
                self._log.register_secret(output.name, node.value or node.name)

    def _output_locators(self) -> dict[str, Locator]:
        """Output name -> the locator in force for this run, after any tenant overlay."""
        return {o.name: o.locator for o in self._cap.outputs if o.locator is not None}

    def _sensitive_locators(self) -> list[Locator]:
        return [
            o.locator
            for o in self._cap.outputs
            if o.locator is not None and o.sensitivity in ("pii", "secret")
        ]

    async def _extract_all(self, ctx: _Ctx) -> None:
        """Read declared outputs from wherever the session ended up."""
        for step in self._cap.steps:
            if step.action is ActionKind.EXTRACT and step.output not in ctx.outputs:
                trace = StepTrace(
                    step_id=step.id,
                    intent=step.intent,
                    action=str(step.action),
                    status=StepStatus.OK,
                )
                await self._extract(step, ctx, trace)

    def _escalated_result(
        self,
        reason: EscalationReason,
        step: Step | None,
        ctx: _Ctx,
        intervention: Intervention | None,
        handoff: Any = None,
    ) -> Escalated:
        return Escalated(
            capability=self._cap.qualified_name,
            tenant=self._tenant,
            steps=ctx.traces,
            drift_suspected=ctx.drift,
            intervention=InterventionRef(
                id=intervention.id if intervention else f"int_{self._log.run_id}",
                reason=reason,
                step_id=step.id if step else None,
                raised_at=intervention.raised_at if intervention else None,
                resolved_by=handoff.operator if handoff else None,
                human_actions=[a.describe() for a in handoff.actions] if handoff else [],
                operator_url=intervention.operator_url if intervention else None,
            ),
        )

    def _error(
        self,
        cls: ErrorClass,
        step: Step | None,
        expected: str,
        observed: str,
        ctx: _Ctx,
        locator: Locator | None = None,
    ) -> Failed:
        if ctx.observation is not None:
            self._register_visible_secrets(ctx)
            self._log.write_failure_context(
                aria_snapshot=ctx.observation.aria_yaml,
                observations=[ctx.observation.model_dump(mode="json")],
                summary=(
                    f"{cls} at {step.id if step else 'success check'}\n"
                    f"expected: {expected}\nobserved: {observed}"
                ),
            )
        self._log.error(str(cls), step_id=step.id if step else None, expected=expected)
        return Failed(
            capability=self._cap.qualified_name,
            tenant=self._tenant,
            steps=ctx.traces,
            drift_suspected=ctx.drift,
            error=ReplayError(
                error_class=cls,
                step_id=step.id if step else None,
                step_intent=step.intent if step else None,
                expected=expected,
                observed=observed,
                locator_explain=locator.explain() if locator else None,
            ),
        )

    def _fail(
        self,
        started: float,
        cls: ErrorClass,
        *,
        expected: str,
        observed: str,
        ctx: _Ctx | None = None,
    ) -> Failed:
        result = Failed(
            capability=self._cap.qualified_name,
            tenant=self._tenant,
            steps=ctx.traces if ctx else [],
            error=ReplayError(error_class=cls, expected=expected, observed=observed),
        )
        return self._finish(result, started, ctx)  # type: ignore[return-value]

    def _finish(self, result: ReplayResult, started: float, ctx: _Ctx | None) -> ReplayResult:
        result.duration_ms = int((time.monotonic() - started) * 1000)
        if ctx is not None:
            result.steps = ctx.traces
            result.drift_suspected = result.drift_suspected or ctx.drift
        result.evidence = EvidenceRef(
            run_id=self._log.run_id,
            run_dir=str(self._log.dir),
            events_path=str(self._log.dir / "events.jsonl"),
            failure_dir=str(self._log.failure_dir) if self._log.failure_dir.exists() else None,
        )
        self._log.event(
            EventType.RUN_FINISHED,
            status=result.status,
            duration_ms=result.duration_ms,
            drift_suspected=result.drift_suspected,
        )
        return result

    # ------------------------------------------------------------------ helpers

    def _validate_inputs(self, inputs: dict[str, str]) -> dict[str, str]:
        values: dict[str, str] = {}
        for param in self._cap.inputs:
            raw = inputs.get(param.name, param.default)
            if raw in (None, "") and param.required:
                raise InputValidationError(f"missing required input {param.name!r}")
            if raw in (None, ""):
                continue
            text = str(raw)
            if param.pattern and not re.fullmatch(param.pattern, text):
                raise InputValidationError(f"{param.name}={text!r} does not match {param.pattern}")
            if param.enum and text not in param.enum:
                raise InputValidationError(f"{param.name}={text!r} not in {param.enum}")
            values[param.name] = text

        unknown = set(inputs) - {p.name for p in self._cap.inputs}
        if unknown:
            raise InputValidationError(f"unknown input(s): {sorted(unknown)}")
        return values

    async def _resolve_with_retry(
        self, locator: Locator, step: Step, ctx: _Ctx, trace: StepTrace
    ) -> Resolution:
        """Resolve, and on failure re-observe a bounded number of times before giving up.

        This is the brief's "wait/retry a transient load", and it is deliberately *not* a
        sleep: re-observing runs the surface's settle logic again, so each attempt waits
        on a load-state condition. A screen that takes longer than one settle window -
        a report that runs a slow query, say - resolves on the second attempt instead of
        being reported as a missing control.

        The retry is capped by the step's own `retries`, and every attempt is recorded so
        a capability that quietly needs three tries every run shows up in the evidence
        rather than looking healthy.
        """
        resolution = await self._resolve(locator, ctx)
        if resolution.outcome is ResolveOutcome.RESOLVED:
            return resolution

        for attempt in range(1, step.retries + 1):
            self._log.event(
                EventType.RECOVERY,
                step_id=step.id,
                recovery="transient-load",
                attempt=attempt,
                detail=f"{locator.describe} did not resolve; re-observing",
            )
            ctx.observation = await self._surface.observe()
            resolution = await self._resolve(locator, ctx)
            trace.recoveries.append(
                RecoveryRecord(
                    recovery_id="transient-load",
                    attempt=attempt,
                    succeeded=resolution.outcome is ResolveOutcome.RESOLVED,
                    detail="waited for a slow screen and re-resolved",
                )
            )
            if resolution.outcome is ResolveOutcome.RESOLVED:
                trace.status = StepStatus.RECOVERED
                return resolution
        return resolution

    async def _resolve(self, locator: Locator, ctx: _Ctx) -> Resolution:
        assert ctx.observation is not None
        bound = bind(locator, ctx.values)
        resolution = await self._surface.resolve(bound, ctx.observation)
        self._log.event(
            EventType.LOCATOR_RESOLVED,
            target=bound.describe,
            outcome=str(resolution.outcome),
            winning_tier=int(resolution.winning_tier) if resolution.winning_tier else None,
        )
        return resolution

    def _value_for(self, step: Step, ctx: _Ctx) -> str | None:
        if step.value is None:
            return None
        if step.value.param:
            return ctx.values.get(step.value.param, "")
        if step.value.secret_ref:
            # Resolved from the secret store at execution time. The artifact only ever
            # held the name.
            return self._secrets.get(step.value.secret_ref, "")
        return step.value.literal

    def _expand(self, url: str | None, ctx: _Ctx) -> str | None:
        if not url:
            return None
        out = url.replace("{{base_url}}", self._base_url)
        for name, value in ctx.values.items():
            out = out.replace(f"{{{{{name}}}}}", value)
        return out

    def _check_drift(self, ctx: _Ctx) -> None:
        recorded = self._cap.provenance.surface_fingerprint
        if not recorded or ctx.observation is None:
            return
        if ctx.observation.fingerprint != recorded:
            # Never fails a run on its own. The screen's shape differs from the
            # recording, which is worth knowing per tenant well before it becomes an
            # outage - but plenty of benign changes move a fingerprint.
            ctx.drift = True

    @staticmethod
    def _summarise(observation: Observation) -> str:
        text = screen_text(observation).replace("\n", " | ")
        return text[:240] + ("..." if len(text) > 240 else "")
