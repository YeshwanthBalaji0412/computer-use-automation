"""Recording -> Capability.

Deterministic and LLM-free. The model's job ended when the run did; turning what it did
into a contract is a mechanical transformation, and keeping it mechanical is what makes
the result reviewable and reproducible.

Six passes:

1. **Prune.** Drop actions that failed or changed nothing. A model that clicked a dead
   link, went back, and tried again contributes one step, not three.
2. **Parameterise.** Values the model flagged as parameters become `{{name}}` in both the
   step value and any locator that mentioned them - a tier-4 row key recorded as
   "Member ID = 100042" is otherwise a capability that works for exactly one member.
3. **Synthesise checkpoints.** Whatever appeared on screen that was not there before is,
   by construction, evidence the step worked. That becomes `post_assert`.
4. **Merge outcomes.** What the model flagged during the run, plus a global library every
   capability inherits (session expiry, server error) so no author has to remember them.
5. **Attach recoveries.** Same idea: the interstitials and transient conditions that are
   properties of the platform rather than of this particular flow.
6. **Validate.** Run the whole thing through the Pydantic model so a capability with a
   dangling parameter reference fails here, at authoring time, rather than mid-run in a
   browser three weeks later.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from cua.discovery.recorder import RecordedAction, Recorder
from cua.locator.generate import CONTENT_ROLES, bind, parameterise
from cua.policy.engine import load_policy
from cua.policy.risk import classify
from cua.schema.capability import (
    ActionKind,
    ApprovalStatus,
    Assertion,
    AssertionKind,
    Capability,
    InputParam,
    KnownOutcome,
    OutcomeSeverity,
    OutputField,
    ParamType,
    ProductRef,
    Provenance,
    Recovery,
    RecoveryAction,
    RecoveryActionKind,
    RiskClass,
    Sensitivity,
    Step,
    StepValue,
    TargetRef,
    WaitCondition,
    WaitKind,
)
from cua.schema.locator import Locator, LocatorStrategy, NameMatch, Tier

_TYPE_MAP = {
    "text": ParamType.STRING,
    "currency": ParamType.CURRENCY,
    "integer": ParamType.INTEGER,
    "number": ParamType.NUMBER,
    "date": ParamType.DATE,
}

_PARSE_MAP = {
    "text": "text",
    "currency": "currency",
    "integer": "integer",
    "number": "number",
    "date": "date",
}


def product_outcomes(
    vendor: str = "corelink", app: str = "servicing-console"
) -> list[KnownOutcome]:
    """Outcomes that belong to the *vendor product*, not to any one capability.

    "No records found", "not authorized" and "session timed out" are properties of the
    CoreLink search and auth screens: every capability that touches them can hit them,
    and every one would otherwise have to rediscover them. Keying the catalogue by
    product rather than by capability is the same move the artifact schema makes - author
    against the product, specialise per tenant - and it means a single discovery run that
    only walked the happy path still produces a capability that handles the unhappy ones.

    A capability adds its *own* outcomes on top, via `note_outcome` during discovery.
    Nothing here prevents that; this is the floor, not the ceiling.

    Each of these is a legitimate answer the caller needs, which is why they are
    outcomes rather than errors. Conflating "not found" with "not authorized" in
    particular would be a compliance problem, not just a bug - one means the record does
    not exist, the other means it does and this operator may not see it.
    """
    return [
        KnownOutcome(
            code="SESSION_EXPIRED",
            message="The operator session timed out.",
            severity=OutcomeSeverity.WARN,
            terminal=False,  # recoverable: re-authenticate and retry
            detect=Assertion(
                kind=AssertionKind.TEXT_PRESENT,
                pattern=r"(?i)(session (has )?(timed out|ended)|please sign in again)",
                describe="the content frame bounced to a sign-in form",
            ),
        ),
        KnownOutcome(
            code="MEMBER_NOT_FOUND",
            message="No member exists with that identifier.",
            severity=OutcomeSeverity.INFO,
            terminal=True,
            returns={"found": False},
            detect=Assertion(
                kind=AssertionKind.TEXT_PRESENT,
                pattern=r"(?i)no\s+(member\s+)?records?\s+found",
                describe="the search screen reported no matching records",
            ),
        ),
        KnownOutcome(
            code="PERMISSION_DENIED",
            message="The operator's role is not entitled to view this record.",
            severity=OutcomeSeverity.WARN,
            terminal=True,
            returns={"found": True, "authorized": False},
            detect=Assertion(
                kind=AssertionKind.TEXT_PRESENT,
                pattern=r"(?i)(not authoriz|access restricted|not entitled)",
                describe="the console refused access to this record",
            ),
        ),
        KnownOutcome(
            code="VALIDATION_REJECTED",
            message="The application rejected the input as malformed.",
            severity=OutcomeSeverity.INFO,
            terminal=True,
            detect=Assertion(
                kind=AssertionKind.TEXT_PRESENT,
                pattern=r"(?i)invalid\s+member\s+id|enter a \d+-digit",
                describe="the search screen showed a validation message",
            ),
        ),
        KnownOutcome(
            code="DUPLICATE_RECORD",
            message="A record with those details already exists; nothing was created.",
            severity=OutcomeSeverity.INFO,
            terminal=True,
            detect=Assertion(
                kind=AssertionKind.TEXT_PRESENT,
                pattern=r"(?i)already exists",
                describe="the pre-flight duplicate check fired",
            ),
        ),
        KnownOutcome(
            code="APP_ERROR",
            message="The application returned a server error.",
            # Declared, so a caller knows it can happen - but `error`, so it comes back
            # through the failure channel. There is no balance to report on a 500, and
            # calling that an answer is the same conflation as calling "no such member" a
            # crash, pointed the other way.
            severity=OutcomeSeverity.ERROR,
            terminal=True,
            detect=Assertion(
                kind=AssertionKind.TEXT_PRESENT,
                pattern=r"(?i)server error in .* application|unhandled exception",
                describe="the application's error page",
            ),
        ),
    ]


def global_recoveries() -> list[Recovery]:
    """Platform-level recoveries, inherited by every capability."""
    return [
        Recovery(
            id="dismiss-known-interstitial",
            describe="Dismiss the scheduled-maintenance notice, which is informational.",
            # Bounded but not one-shot: an informational notice can reappear on later
            # screens in the same flow, and exhausting after the first would escalate a
            # condition already known to be safe. Still capped, so a notice that cannot
            # be dismissed escalates rather than looping.
            max_attempts=3,
            detect=Assertion(
                kind=AssertionKind.TEXT_PRESENT,
                pattern=r"(?i)scheduled maintenance",
                describe="the maintenance notice dialog",
            ),
            action=RecoveryAction(
                kind=RecoveryActionKind.DISMISS,
                locator=Locator(
                    describe="the dismiss button on the maintenance notice",
                    any_frame=True,
                    strategies=[
                        LocatorStrategy(
                            tier=Tier.ROLE_NAME_NORMALISED,
                            role="button",
                            name="Close",
                            name_match=NameMatch.NORMALISED,
                            note="the notice's acknowledge button",
                        )
                    ],
                ),
            ),
        ),
        Recovery(
            # Named for the condition, not the cure: `_recovery_for` pairs a non-terminal
            # known outcome with its recovery by matching SESSION_EXPIRED -> session-expired.
            id="session-expired",
            describe="The session expired mid-flow. Sign in again and pick up where the "
            "flow started, because re-authenticating lands on the entry screen rather "
            "than the one the step failed on.",
            # Once. A session that will not stay established is an environment problem,
            # and a second attempt would only delay the escalation that says so.
            max_attempts=1,
            detect=Assertion(
                kind=AssertionKind.TEXT_PRESENT,
                pattern=r"(?i)(session has timed out|please sign in again)",
                describe="the sign-in screen, reached without asking for it",
            ),
            # No action of its own: the sign-in steps are already the first steps of the
            # capability, so *restarting from them* is what re-authenticating means. The
            # alternative - a bespoke RE_AUTHENTICATE action holding a second copy of the
            # credentials and the login form's locators - would be the same steps written
            # twice, and the copy would rot.
            action=RecoveryAction(kind=RecoveryActionKind.WAIT_AND_RETRY),
            # Filled in by the compiler, which is the only place that knows the step ids.
            restart_from_step=None,
        ),
        Recovery(
            id="transient-load",
            describe="Wait and retry once for a slow screen.",
            max_attempts=2,
            backoff_ms=[750, 2000],
            detect=Assertion(
                kind=AssertionKind.TEXT_PRESENT,
                pattern=r"(?!)never-matches-by-detector",  # driven by step timeout, not text
                describe="a step timed out waiting for the screen",
            ),
            action=RecoveryAction(kind=RecoveryActionKind.WAIT_AND_RETRY),
        ),
    ]


def compile_capability(
    recorder: Recorder,
    *,
    capability_id: str,
    description: str,
    display_name: str,
    goal: str,
    tenant: str,
    base_url: str,
    version: str = "1.0.0",
    model: str | None = None,
    prompt_version: str | None = None,
    run_id: str | None = None,
    vendor: str = "corelink",
    app: str = "servicing-console",
    app_version_range: str = "*",
) -> Capability:
    params = recorder.parameters

    inputs = _build_inputs(recorder, params)
    outputs = _build_outputs(recorder, params)
    steps = _build_steps(recorder, params, base_url)
    success = _build_success_condition(recorder, params, outputs)

    capability = Capability(
        id=capability_id,
        version=version,
        display_name=display_name or capability_id,
        description=description or goal,
        target=TargetRef(
            surface="legacy-web",
            product=ProductRef(vendor=vendor, app=app, version_range=app_version_range),
            entry_url_pattern=_canonicalise(recorder.entry_url, base_url, params),
            requires_auth=True,
        ),
        # Never born approved. Unattended execution is a decision a human makes after
        # reading the artifact, and `stability` gives them evidence to make it with.
        status=ApprovalStatus.DRAFT,
        risk_class=_overall_risk(steps),
        idempotent=_overall_risk(steps) is RiskClass.READ_ONLY,
        inputs=inputs,
        outputs=outputs,
        steps=steps,
        success_condition=success,
        known_outcomes=_merge_outcomes(recorder),
        recoveries=_recoveries_for(steps),
        provenance=Provenance(
            discovered_by="llm",
            model=model,
            prompt_version=prompt_version,
            discovery_run_id=run_id,
            recorded_at=datetime.now(UTC),
            recorded_against_tenant=tenant,
            surface_fingerprint=recorder.entry_fingerprint or recorder.final_fingerprint,
        ),
    )
    # Validation is not a formality here: it is what catches a step referencing a
    # parameter the compiler failed to create, at authoring time rather than at 3am.
    return Capability.model_validate(capability.model_dump())


# ---------------------------------------------------------------- passes


def _build_inputs(recorder: Recorder, params: dict[str, str]) -> list[InputParam]:
    out: list[InputParam] = []
    for action in recorder.actions:
        if not (action.is_parameter and action.parameter_name):
            continue
        if any(p.name == action.parameter_name for p in out):
            continue
        value = params.get(action.parameter_name, "")
        out.append(
            InputParam(
                name=action.parameter_name,
                type=ParamType.STRING,
                required=True,
                description=action.why or f"Value for {action.parameter_name}.",
                pattern=_infer_pattern(value),
                sensitivity=Sensitivity.INTERNAL,
                example=value or None,
            )
        )
    return out


def _infer_pattern(value: str) -> str | None:
    """A cheap input contract from the one example we have.

    Deliberately conservative - only fixed-length digit strings get a pattern. Guessing
    a regex from a single sample is how you end up rejecting valid input, and a wrong
    contract is worse than none.
    """
    if value.isdigit() and 4 <= len(value) <= 12:
        return rf"^\d{{{len(value)}}}$"
    return None


def _build_outputs(recorder: Recorder, params: dict[str, str]) -> list[OutputField]:
    outputs: list[OutputField] = []
    extracts = [a for a in recorder.actions if a.action is ActionKind.EXTRACT and a.output_name]

    for action in extracts:
        if any(o.name == action.output_name for o in outputs):
            continue
        locator = action.target
        if locator is not None and params:
            locator = parameterise(locator, params)
        outputs.append(
            OutputField(
                name=action.output_name or "value",
                type=_TYPE_MAP.get(action.output_type, ParamType.STRING),
                required=True,
                description=action.why,
                sensitivity=Sensitivity.PII if action.output_sensitive else Sensitivity.INTERNAL,
                locator=locator,
                parse=_PARSE_MAP.get(action.output_type, "text"),  # type: ignore[arg-type]
            )
        )
    return outputs


def _build_steps(recorder: Recorder, params: dict[str, str], base_url: str) -> list[Step]:
    """Risk is classified here, at compile time, and written into the artifact.

    Doing it now rather than at run time means the classification is *reviewable*: the
    person approving a capability sees exactly which steps the system considers writes,
    and can reject the artifact if it disagrees with them. The runtime re-derives it as
    well, and an artifact may only ever declare a step *more* dangerous than inferred.
    """
    policy = load_policy()
    steps: list[Step] = []

    for index, action in enumerate(recorder.effective_actions, start=1):
        step_id = f"s{index}"
        locator = action.target
        if locator is not None and params:
            locator = parameterise(locator, params)

        value = _step_value(action, params)
        post = _post_assert(action, locator)
        risk, _reason = classify(
            action.action,
            action.control_name,
            irreversible_signals=policy.irreversible_signals,
            reversible_signals=policy.reversible_signals,
        )

        steps.append(
            Step(
                id=step_id,
                intent=action.why or f"{action.action} {action.element_describe}".strip(),
                action=action.action,
                target=locator,
                control_name=action.control_name,
                value=value,
                url=(
                    _canonicalise(action.url, base_url, params)
                    if action.action is ActionKind.NAVIGATE
                    else None
                ),
                output=action.output_name if action.action is ActionKind.EXTRACT else None,
                risk=risk,
                wait_for=[WaitCondition(kind=WaitKind.NETWORK_IDLE, timeout_ms=10_000)],
                post_assert=post,
            )
        )
    return steps


def _step_value(action: RecordedAction, params: dict[str, str]) -> StepValue | None:
    if action.action not in (ActionKind.FILL, ActionKind.SELECT):
        return None
    if action.is_secret:
        # Schema decision 5, enforced rather than merely documented: a credential is
        # referenced by name and resolved from the secret store at execution time, so
        # it is structurally impossible for one to be committed inside an artifact.
        return StepValue(secret_ref=action.secret_ref or "corelink.secret")
    if action.is_parameter and action.parameter_name:
        return StepValue(param=action.parameter_name)
    return StepValue(literal=action.value_literal or "")


def _post_assert(action: RecordedAction, locator: Locator | None) -> list[Assertion]:
    """Pass 3: whatever appeared that was not there before is evidence the step worked.

    Weak on its own, which is why it is `any_of` over a couple of candidates rather than
    a single brittle claim. The strong checkpoints come from the model's explicit
    `assert_state` calls and end up in `success_condition`.
    """
    if not action.appeared:
        return []
    candidates: list[Assertion] = []
    for entry in action.appeared[:3]:
        role, _, name = entry.partition(":")
        if not name or len(name) > 60:
            continue
        candidates.append(
            Assertion(
                kind=AssertionKind.TEXT_PRESENT,
                pattern=_escape(name),
                describe=f"{role} {name!r} appeared after this step",
            )
        )
    if not candidates:
        return []
    if len(candidates) == 1:
        return candidates
    return [
        Assertion(
            kind=AssertionKind.ANY_OF,
            describe="the screen advanced as recorded",
            of=candidates,
        )
    ]


def _build_success_condition(
    recorder: Recorder, params: dict[str, str], outputs: list[OutputField]
) -> Assertion | None:
    """The model's final `assert_state`, plus a requirement that outputs were readable.

    Reaching the right screen but failing to read the value you were asked for is not
    success - it is `OUTPUT_EXTRACTION_FAILED`, and the success condition has to be
    strict enough to say so.
    """
    parts: list[Assertion] = []

    if recorder.checkpoints:
        final = recorder.checkpoints[-1]
        for locator in final.locators[:3]:
            bound = parameterise(locator, params) if params else locator
            assertion = _checkpoint_assertion(bound, final.describe)
            if assertion is not None:
                parts.append(assertion)

    for output in outputs:
        if output.locator is not None:
            # By name, not by embedded locator: a tenant that renders this value
            # somewhere else overrides the *output*, and the success condition follows
            # automatically rather than needing an override of its own.
            parts.append(
                Assertion(
                    kind=AssertionKind.OUTPUT_PRESENT,
                    output=output.name,
                    describe=f"output {output.name!r} is readable",
                )
            )

    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return Assertion(
        kind=AssertionKind.ALL_OF, describe="the capability reached its goal state", of=parts
    )


def _checkpoint_assertion(locator: Locator, describe: str) -> Assertion | None:
    """Turn one recorded checkpoint into an assertion that survives a different tenant.

    The model records a checkpoint by pointing at elements on the screen it reached. Two
    of those pointers are traps, and both were found by replaying a Meridian recording at
    Lakeside - which renders the same member detail as a definition list rather than a
    nested table, so *there are no cells at all*.

    **A content element's role is the tenant's markup choice.** `cell` versus `definition`
    says nothing about whether the run succeeded. Asserting on it makes the success
    condition a claim about HTML structure, which is the exact thing this whole system
    refuses to depend on everywhere else. The tenant-neutral form of "the member id is on
    this screen" is text, not an element.

    **A content element's accessible name is the data.** A checkpoint on `cell
    'J. RIVERA'` verifies that one member's name is displayed - so it is really asserting
    the answer, and it only holds for the member who happened to be recorded. Dropped:
    a success condition that is true only for the recording is worse than one less clause.

    Structural elements - a heading, a landmark, a button - keep `ELEMENT_PRESENT`. A
    heading is a semantic role rather than a markup accident, it survives the layout
    change, and the ladder already carries a text-based fallback tier for it.
    """
    role = next((s.role for s in locator.strategies if s.role), "")
    if role not in CONTENT_ROLES:
        return Assertion(
            kind=AssertionKind.ELEMENT_PRESENT,
            locator=locator,
            describe=f"{describe}: {locator.describe}",
        )

    name = next((s.name for s in locator.strategies if s.name), "")
    if "{{" not in name:
        # A literal member value. Asserting it would pin the capability to one member.
        return None

    return Assertion(
        kind=AssertionKind.TEXT_PRESENT,
        pattern=re.escape(name).replace(r"\{\{", "{{").replace(r"\}\}", "}}"),
        describe=f"{describe}: the text {name} appears on screen",
    )


def _recoveries_for(steps: list[Step]) -> list[Recovery]:
    """Bind the platform recoveries to this capability's own step ids.

    Only `re-authenticate` needs it, and only because "where do I pick up after signing
    in again" is a fact about the recorded flow rather than about the platform. If a
    capability has no steps to go back to, the recovery is dropped rather than shipped
    pointing at nothing - the schema validates that reference on load, so a dangling one
    would make the artifact unloadable.
    """
    out: list[Recovery] = []
    for recovery in global_recoveries():
        if recovery.id == "session-expired":
            if not steps:
                continue
            recovery = recovery.model_copy(update={"restart_from_step": steps[0].id})
        out.append(recovery)
    return out


def _merge_outcomes(recorder: Recorder) -> list[KnownOutcome]:
    outcomes = list(product_outcomes())
    seen = {o.code for o in outcomes}

    for recorded in recorder.outcomes:
        if recorded.code in seen or not recorded.text_pattern:
            continue
        outcomes.append(
            KnownOutcome(
                code=recorded.code,
                message=recorded.describe,
                severity=OutcomeSeverity.INFO,
                terminal=True,
                detect=Assertion(
                    kind=AssertionKind.TEXT_PRESENT,
                    pattern=_escape(recorded.text_pattern),
                    describe=recorded.describe,
                ),
            )
        )
        seen.add(recorded.code)
    return outcomes


def _overall_risk(steps: list[Step]) -> RiskClass:
    if any(s.risk is RiskClass.IRREVERSIBLE_WRITE for s in steps):
        return RiskClass.IRREVERSIBLE_WRITE
    if any(s.risk is RiskClass.REVERSIBLE_WRITE for s in steps):
        return RiskClass.REVERSIBLE_WRITE
    return RiskClass.READ_ONLY


def _canonicalise(url: str | None, base_url: str, params: dict[str, str]) -> str:
    """Strip the tenant's deployment out of a URL so the artifact describes a *product*.

    `http://localhost:4000/tenants/meridian/frame/content?screen=search` becomes
    `{{base_url}}/frame/content?screen=search`, and any recorded parameter value in the
    query string becomes its placeholder. Without this, an artifact is bound to one
    tenant's host and one member's id.
    """
    if not url:
        return ""
    out = url
    if base_url and out.startswith(base_url):
        out = "{{base_url}}" + out[len(base_url) :]
    for name, value in params.items():
        if value:
            out = out.replace(value, f"{{{{{name}}}}}")
    return out


def _escape(text: str) -> str:
    """Escape regex metacharacters, but keep whitespace flexible and the result readable.

    `re.escape` turns "No member records found" into "No\\ member\\ records\\ found",
    which is correct and unreadable - and artifacts are reviewed by humans. Collapsing
    runs of whitespace to `\\s+` is also more robust: legacy pages wrap and re-indent
    text between renders without changing what it says.
    """
    import re

    return r"\s+".join(re.escape(part) for part in text.strip().split())


__all__ = ["bind", "compile_capability", "global_recoveries", "product_outcomes"]
