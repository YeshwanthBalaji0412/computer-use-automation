"""Two-layer enforcement.

**Layer 1 - action level.** Every action, from any actor, passes `check()` before it
happens. That includes the model's tool calls, replay's steps, and navigation performed
by a human during a handoff. A blocked action is returned to the caller as a refusal
with a reason, not raised: the model receives "blocked by policy: ..." as a tool result
and can adapt, rather than the run unwinding.

**Layer 2 - network level.** `allows_request()` backs a Playwright route handler that
aborts anything heading somewhere the policy does not permit.

The second layer is not redundant, and the distinction is worth being able to state:
*layer 1 controls what the agent chooses to do; layer 2 controls what the page can do on
the agent's behalf.* A 302 to an external identity provider, or a script injected into a
legacy app's free-text field, moves data without any action being requested at all.
Prompt injection through application data is a real threat in this environment, and only
the network layer stops it.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urlparse

import yaml

from cua.policy.risk import classify
from cua.schema.capability import ActionKind, RiskClass
from cua.schema.policy import ALLOWED, Decision, DecisionKind, Gate, Policy

DEFAULT_POLICY_PATH = Path(__file__).parent / "policy.default.yaml"


def load_policy(path: Path | None = None) -> Policy:
    raw = yaml.safe_load((path or DEFAULT_POLICY_PATH).read_text())
    return Policy.model_validate(raw)


class PolicyEngine:
    """Holds the policy and answers questions about it. No I/O, no browser."""

    def __init__(self, policy: Policy, *, caller_approved: bool = False) -> None:
        self._policy = policy
        #: The caller explicitly passed an approval for this invocation (`--approve`).
        #: Never inferred, never sticky across runs.
        self._caller_approved = caller_approved

    @property
    def policy(self) -> Policy:
        return self._policy

    def narrowed(self, **limits: object) -> PolicyEngine:
        """A view of this engine with a capability's own limits intersected in."""
        return PolicyEngine(
            self._policy.intersect(**limits),  # type: ignore[arg-type]
            caller_approved=self._caller_approved,
        )

    # ------------------------------------------------------------------ layer 1

    def check(
        self,
        action: ActionKind,
        *,
        url: str | None = None,
        control_name: str = "",
        declared_risk: RiskClass | None = None,
        capability_approved: bool = False,
        during_discovery: bool = False,
    ) -> Decision:
        """Decide whether this action may proceed. Called before every single action."""
        if action in self._policy.denied_actions:
            return Decision(
                kind=DecisionKind.BLOCK,
                rule="denied_actions",
                reason=f"action type {action} is denied by policy",
            )
        if self._policy.allowed_actions and action not in self._policy.allowed_actions:
            return Decision(
                kind=DecisionKind.BLOCK,
                rule="allowed_actions",
                reason=f"action type {action} is not in the allowlist",
            )

        if url is not None:
            verdict = self.check_url(url)
            if not verdict.allowed:
                return verdict

        risk, reason = classify(
            action,
            control_name,
            irreversible_signals=self._policy.irreversible_signals,
            reversible_signals=self._policy.reversible_signals,
            declared=declared_risk,
        )
        return self._gate(risk, reason, capability_approved, during_discovery)

    def _gate(
        self,
        risk: RiskClass,
        reason: str,
        capability_approved: bool,
        during_discovery: bool,
    ) -> Decision:
        gate = self._policy.risk_gates.get(risk, Gate.REQUIRE_APPROVAL)

        if gate is Gate.ALLOW:
            return ALLOWED
        if gate is Gate.DENY:
            return Decision(kind=DecisionKind.BLOCK, rule=f"risk_gates.{risk}", reason=reason)

        if during_discovery or gate is Gate.ESCALATE:
            # During discovery the model is by definition operating on a UI it does not
            # yet understand. That is the worst possible moment to let it press
            # "Confirm and Open Account", so a risky action becomes a human decision.
            # Reusing the escalation path here means one mechanism covers two rules.
            return Decision(
                kind=DecisionKind.ESCALATE,
                rule=f"risk_gates.{risk}",
                reason=f"{reason}; risky actions are escalated during discovery",
                required_approval=["human approval via intervention"],
            )

        # Replay: fail closed. Every condition must hold, and the ones that do not are
        # named so the caller knows exactly what to fix.
        missing: list[str] = []
        if not capability_approved:
            missing.append("capability.status == approved")
        if not self._caller_approved:
            missing.append("--approve")
        if missing:
            return Decision(
                kind=DecisionKind.BLOCK,
                rule=f"risk_gates.{risk}",
                reason=reason,
                required_approval=missing,
            )
        return ALLOWED

    # ------------------------------------------------------------------ layer 2

    def check_url(self, url: str) -> Decision:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path or "/"

        # Explicit denies win over the allowlist, always.
        for pattern in self._policy.denied_path_patterns:
            if fnmatch(path, pattern):
                return Decision(
                    kind=DecisionKind.BLOCK,
                    rule="denied_path_patterns",
                    reason=f"path {path!r} matches denied pattern {pattern!r}",
                )

        if origin not in self._policy.allowed_origins:
            return Decision(
                kind=DecisionKind.BLOCK,
                rule="allowed_origins",
                reason=f"origin {origin!r} is not on the allowlist",
            )

        patterns = self._policy.allowed_path_patterns
        if patterns and not any(fnmatch(path, p) for p in patterns):
            return Decision(
                kind=DecisionKind.BLOCK,
                rule="allowed_path_patterns",
                reason=f"path {path!r} matches no allowed pattern",
            )

        return ALLOWED

    def allows_request(self, url: str) -> bool:
        """Backs the network-level route handler.

        Deliberately origin-only: a page legitimately fetches paths the agent would
        never navigate to (stylesheets, images, XHR). Blocking those would break the
        application. What must not happen is a request to an origin outside the
        allowlist, and that is what this stops.
        """
        parsed = urlparse(url)
        if parsed.scheme in ("data", "blob", "about"):
            return True
        return f"{parsed.scheme}://{parsed.netloc}" in self._policy.allowed_origins
