"""The guardrail model.

Policy is **data**, not code. It lives in `policy.default.yaml` so a compliance reviewer
can read what the automation is permitted to do without reading Python, and so a change
to it shows up as a reviewable diff rather than a commit somewhere in an executor.

Two properties are deliberate and worth defending:

**Capability policy can only narrow, never widen.** A capability declares its own limits,
and they are *intersected* with the global policy at run time. A tampered or
over-enthusiastic artifact cannot grant itself an origin or an action type the
institution has not allowed. `Policy.intersect` is where that is enforced, in one place.

**Risky actions fail closed.** The gate for an irreversible write requires every
condition to hold. A missing approval is a `blocked` result with the missing conditions
named - never a warning that the run sails past. At a bank an unreviewed irreversible
write is a regulatory incident, not a log line.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from cua.schema.capability import ActionKind, RiskClass


class Gate(StrEnum):
    """What to do when an action of a given risk class is attempted."""

    ALLOW = "allow"
    #: Permit only when the capability is approved AND the caller passed an approval.
    REQUIRE_APPROVAL = "require_approval"
    #: Pause and hand to a human. Used during discovery, where the model is by
    #: definition operating on a UI it does not yet understand.
    ESCALATE = "escalate"
    DENY = "deny"


class RedactionPolicy(BaseModel):
    """What the redactor does. Separate from the allowlist because it governs data
    leaving the process rather than actions entering the application."""

    enabled: bool = True
    #: Mask values of fields the capability declares as pii/secret. Precise and
    #: complete for anything the schema knows about.
    declared_fields: bool = True
    #: Backstop regexes for data no one declared. Best-effort by nature.
    patterns: bool = True
    #: Replace a matched value with a stable HMAC so two occurrences can be correlated
    #: in a log without either being readable.
    hash_correlation: bool = True


class Policy(BaseModel):
    """The institution's rules. Loaded once, enforced at every action."""

    #: Layer 1: which origins the agent may drive at all.
    allowed_origins: list[str] = Field(default_factory=list)
    #: Glob patterns, matched against the URL path. Empty means "any path on an
    #: allowed origin".
    allowed_path_patterns: list[str] = Field(default_factory=list)
    #: Checked first and wins over the allowlist - an explicit deny is never overridden.
    denied_path_patterns: list[str] = Field(default_factory=list)

    allowed_actions: list[ActionKind] = Field(default_factory=list)
    denied_actions: list[ActionKind] = Field(default_factory=list)

    max_steps_per_run: int = 40
    max_run_duration_ms: int = 300_000

    risk_gates: dict[RiskClass, Gate] = Field(
        default_factory=lambda: {
            RiskClass.READ_ONLY: Gate.ALLOW,
            RiskClass.REVERSIBLE_WRITE: Gate.REQUIRE_APPROVAL,
            RiskClass.IRREVERSIBLE_WRITE: Gate.REQUIRE_APPROVAL,
        }
    )

    #: Regexes matched against a control's *accessible name*. Risk is inferred from what
    #: the control says, because that is what a human operator reads before deciding.
    irreversible_signals: list[str] = Field(default_factory=list)
    reversible_signals: list[str] = Field(default_factory=list)

    redaction: RedactionPolicy = Field(default_factory=RedactionPolicy)

    def intersect(
        self,
        *,
        allowed_origins: list[str] | None = None,
        allowed_actions: list[ActionKind] | None = None,
        max_steps: int | None = None,
        max_duration_ms: int | None = None,
    ) -> Policy:
        """Narrow this policy with a capability's own limits.

        Intersection, never union. If a capability names an origin the institution has
        not allowed, the result is the empty intersection for that origin - the
        capability does not get it by asking.
        """
        origins = self.allowed_origins
        if allowed_origins:
            origins = [o for o in allowed_origins if o in self.allowed_origins]

        actions = self.allowed_actions
        if allowed_actions:
            actions = [a for a in allowed_actions if a in self.allowed_actions]

        return self.model_copy(
            update={
                "allowed_origins": origins,
                "allowed_actions": actions,
                "max_steps_per_run": min(self.max_steps_per_run, max_steps or 10**9),
                "max_run_duration_ms": min(self.max_run_duration_ms, max_duration_ms or 10**9),
            }
        )


class DecisionKind(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    #: Permitted, but a human has to say so first.
    ESCALATE = "escalate"


class Decision(BaseModel):
    kind: DecisionKind
    rule: str = Field(default="", description="Which rule decided, for the audit trail.")
    reason: str = ""
    required_approval: list[str] = Field(
        default_factory=list,
        description="What would have to be true for this to be permitted.",
    )

    @property
    def allowed(self) -> bool:
        return self.kind is DecisionKind.ALLOW


ALLOWED = Decision(kind=DecisionKind.ALLOW, rule="default")
