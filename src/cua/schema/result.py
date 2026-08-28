"""What a replay returns.

Five outcomes, not two. The split is the whole point:

    success            the flow completed; typed outputs are attached
    business_outcome   the application gave a legitimate answer we were told to expect
                       ("no such member"). NOT an error. Does not raise.
    escalated          we stopped somewhere it is unsafe to guess; a human was asked
    blocked            policy refused the action before it happened
    failed             something genuinely broke; here is enough detail to debug it

`business_outcome` existing as a peer of `success` rather than a flavour of `failed` is
the design the brief singles out: *"'No such member' is a legitimate answer the caller
needs, not a crash. Conflating the two is the most common design mistake here."*

`blocked` is separate from `failed` for a related reason. A policy refusal is not a
malfunction - the system worked exactly as designed - and a caller's response to it is
different: get approval, don't retry harder.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from cua.schema.locator import Tier


class ErrorClass(StrEnum):
    """Hard failures only. Business outcomes and recoveries never appear here."""

    #: No strategy in the ladder matched. The control is gone or the screen is wrong.
    LOCATOR_UNRESOLVED = "locator_unresolved"
    #: A strategy matched more than one element. We refuse to guess.
    LOCATOR_AMBIGUOUS = "locator_ambiguous"
    #: The action ran but the screen is not what the step promised.
    CHECKPOINT_FAILED = "checkpoint_failed"
    #: Precondition failed before we even acted.
    PRECONDITION_FAILED = "precondition_failed"
    #: The application itself errored (5xx, crash page).
    APP_ERROR = "app_error"
    TIMEOUT_EXCEEDED = "timeout_exceeded"
    #: Reached the declared success state but could not read a required output.
    OUTPUT_EXTRACTION_FAILED = "output_extraction_failed"
    #: A recovery was attempted its maximum number of times and the condition persists.
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    #: The caller passed something the input contract rejects. Fails before launching.
    INVALID_INPUT = "invalid_input"
    SURFACE_ERROR = "surface_error"


class EscalationReason(StrEnum):
    UNKNOWN_DIALOG = "unknown_dialog"
    AMBIGUOUS_STATE = "ambiguous_state"
    RISKY_STEP_NEEDS_APPROVAL = "risky_step_needs_approval"
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    NO_PROGRESS = "no_progress"
    #: A non-idempotent write timed out. The write may or may not have landed, so
    #: retrying could double-post. A human has to look.
    AMBIGUOUS_WRITE_OUTCOME = "ambiguous_write_outcome"
    MODEL_REQUESTED = "model_requested"


class StepStatus(StrEnum):
    OK = "ok"
    RECOVERED = "recovered"
    SKIPPED = "skipped"
    FAILED = "failed"


class RecoveryRecord(BaseModel):
    recovery_id: str
    attempt: int
    succeeded: bool
    detail: str = ""


class StepTrace(BaseModel):
    """Per-step evidence. This is what makes a failure debuggable rather than merely
    reported: which step, what it was trying to do, and how the element was found."""

    step_id: str
    intent: str
    action: str
    status: StepStatus
    duration_ms: int = 0
    winning_tier: Tier | None = None
    tiers_tried: list[Tier] = Field(default_factory=list)
    #: True when a better-ranked strategy failed and a worse one succeeded. The step
    #: still worked - this is a drift warning, not an error.
    locator_degraded: bool = False
    recoveries: list[RecoveryRecord] = Field(default_factory=list)
    screenshot_path: str | None = None
    note: str = ""


class EvidenceRef(BaseModel):
    run_id: str
    run_dir: str
    events_path: str | None = None
    trace_path: str | None = Field(
        default=None, description="Playwright trace.zip - openable in the trace viewer."
    )
    failure_dir: str | None = None


class Outcome(BaseModel):
    code: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class PolicyViolation(BaseModel):
    rule: str
    attempted: str = Field(description="What the agent tried to do.")
    required_approval: list[str] = Field(
        default_factory=list,
        description="What would have to be true for this to be permitted - e.g. "
        "['capability.status==approved', '--approve'].",
    )


class ReplayError(BaseModel):
    """Structured enough to debug from the log alone.

    The brief asks for "what step, what was expected, what was observed". Those are
    three separate fields here on purpose - a single prose message is what turns a
    five-minute diagnosis into an hour of re-running things locally.
    """

    error_class: ErrorClass
    step_id: str | None = None
    step_intent: str | None = None
    expected: str = ""
    observed: str = ""
    locator_explain: str | None = Field(
        default=None, description="The full ladder that was tried, rendered for a human."
    )
    tiers_tried: list[Tier] = Field(default_factory=list)
    detail: str = ""


class InterventionRef(BaseModel):
    id: str
    reason: EscalationReason
    step_id: str | None = None
    raised_at: datetime | None = None
    resolved_by: str | None = None
    human_actions: list[str] = Field(
        default_factory=list,
        description="What the operator did while holding the control lease, recorded "
        "in the same evidence stream as automation's own actions.",
    )
    operator_url: str | None = None


class _Base(BaseModel):
    capability: str = Field(description="Qualified name, e.g. 'member.balance@1.0.0'.")
    tenant: str | None = None
    steps: list[StepTrace] = Field(default_factory=list)
    evidence: EvidenceRef | None = None
    duration_ms: int = 0
    #: Set when the screen skeleton differs from what was recorded. Never fails a run on
    #: its own; it is the signal that this tenant's UI has moved.
    drift_suspected: bool = False


class Success(_Base):
    status: Literal["success"] = "success"
    outputs: dict[str, Any] = Field(default_factory=dict)
    completed_by: Literal["automation", "human"] = "automation"


class BusinessOutcome(_Base):
    """A legitimate answer. Not an error, and it does not raise."""

    status: Literal["business_outcome"] = "business_outcome"
    outcome: Outcome


class Escalated(_Base):
    status: Literal["escalated"] = "escalated"
    intervention: InterventionRef
    resumed_result: ReplayResult | None = Field(
        default=None, description="What happened after the human handed control back."
    )


class Blocked(_Base):
    status: Literal["blocked"] = "blocked"
    policy: PolicyViolation


class Failed(_Base):
    status: Literal["failed"] = "failed"
    error: ReplayError


ReplayResult = Annotated[
    Success | BusinessOutcome | Escalated | Blocked | Failed,
    Field(discriminator="status"),
]

#: Process exit codes, so a shell or an orchestrator can branch without parsing JSON.
#: Note that a business outcome exits 0: it is a successful invocation that happens to
#: report a negative answer.
EXIT_CODES: dict[str, int] = {
    "success": 0,
    "business_outcome": 0,
    "escalated": 3,
    "blocked": 4,
    "failed": 1,
}


# `Escalated.resumed_result` refers to the union declared below it, so the forward
# reference has to be resolved once the name exists.
Escalated.model_rebuild()
