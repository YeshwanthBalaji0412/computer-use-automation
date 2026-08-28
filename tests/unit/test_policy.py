"""Guardrails.

Two properties carry most of the weight and each has a test that would fail loudly if
someone loosened it: a capability cannot widen the institution's policy, and a risky
action fails closed.
"""

from __future__ import annotations

import pytest

from cua.policy.engine import PolicyEngine, load_policy
from cua.policy.risk import classify
from cua.schema.capability import ActionKind, RiskClass
from cua.schema.policy import DecisionKind, Gate, Policy

pytestmark = pytest.mark.unit


@pytest.fixture
def policy() -> Policy:
    return load_policy()


@pytest.fixture
def engine(policy: Policy) -> PolicyEngine:
    return PolicyEngine(policy)


# ------------------------------------------------------------------ the allowlist


def test_the_shipped_policy_loads_and_is_restrictive(policy: Policy) -> None:
    assert policy.allowed_origins, "an empty origin allowlist would permit everything"
    assert policy.risk_gates[RiskClass.IRREVERSIBLE_WRITE] is not Gate.ALLOW


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("http://localhost:4000/tenants/meridian/frame/content", True),
        ("http://localhost:4000/tenants/lakeside/", True),
        ("https://evil.example.com/tenants/meridian/", False),
        ("http://localhost:9999/tenants/meridian/", False),
        ("http://localhost:4000/admin/users", False),
        ("http://localhost:4000/tenants/meridian/admin/keys", False),
        ("http://localhost:4000/tenants/meridian/export/all", False),
        ("http://localhost:4000/internal/debug", False),
    ],
)
def test_url_allowlist(engine: PolicyEngine, url: str, allowed: bool) -> None:
    assert engine.check_url(url).allowed is allowed


def test_denied_paths_win_over_the_allowlist() -> None:
    """An explicit deny must never be overridden by a broader allow, or the deny list
    is decorative."""
    engine = PolicyEngine(
        Policy(
            allowed_origins=["http://localhost:4000"],
            allowed_path_patterns=["/**"],
            denied_path_patterns=["**/admin/**"],
        )
    )
    assert engine.check_url("http://localhost:4000/x/admin/y").allowed is False
    assert engine.check_url("http://localhost:4000/x/normal/y").allowed is True


def test_action_types_outside_the_allowlist_are_blocked(engine: PolicyEngine) -> None:
    decision = engine.check(ActionKind.NAVIGATE, url="https://evil.example/")
    assert decision.kind is DecisionKind.BLOCK
    assert decision.rule == "allowed_origins"


# ------------------------------------------------------------------ narrowing only


def test_a_capability_cannot_widen_the_institutions_policy(engine: PolicyEngine) -> None:
    """The attacker model: a tampered artifact asks for an origin nobody allowed.

    Intersection, never union - so asking does not get it.
    """
    narrowed = engine.narrowed(
        allowed_origins=["https://evil.example", "http://localhost:4000"],
        max_steps=10_000,
    )

    assert narrowed.policy.allowed_origins == ["http://localhost:4000"]
    assert narrowed.check_url("https://evil.example/anything").allowed is False
    assert narrowed.policy.max_steps_per_run <= engine.policy.max_steps_per_run


def test_a_capability_can_narrow_itself(engine: PolicyEngine) -> None:
    narrowed = engine.narrowed(allowed_actions=[ActionKind.NAVIGATE, ActionKind.EXTRACT])
    assert narrowed.check(ActionKind.CLICK, control_name="Search").kind is DecisionKind.BLOCK
    assert narrowed.check(ActionKind.EXTRACT).allowed


# ------------------------------------------------------------------ risk


@pytest.mark.parametrize(
    "action,name,expected",
    [
        (ActionKind.NAVIGATE, "", RiskClass.READ_ONLY),
        (ActionKind.EXTRACT, "", RiskClass.READ_ONLY),
        (ActionKind.FILL, "Member ID", RiskClass.READ_ONLY),
        (ActionKind.SELECT, "Branch", RiskClass.READ_ONLY),
        (ActionKind.CLICK, "Search", RiskClass.READ_ONLY),
        (ActionKind.CLICK, "New Search", RiskClass.READ_ONLY),
        (ActionKind.CLICK, "Continue", RiskClass.REVERSIBLE_WRITE),
        (ActionKind.CLICK, "Save", RiskClass.REVERSIBLE_WRITE),
        (ActionKind.CLICK, "Confirm and Open Account", RiskClass.IRREVERSIBLE_WRITE),
        (ActionKind.CLICK, "Delete Member", RiskClass.IRREVERSIBLE_WRITE),
        (ActionKind.CLICK, "Submit Payment", RiskClass.IRREVERSIBLE_WRITE),
        (ActionKind.CLICK, "Approve", RiskClass.IRREVERSIBLE_WRITE),
        # Unnamed controls are assumed to write. Guessing "harmless" on a control that
        # turns out to post a transaction is the wrong direction to be wrong in.
        (ActionKind.CLICK, "", RiskClass.REVERSIBLE_WRITE),
    ],
)
def test_risk_is_inferred_from_what_the_control_says(
    policy: Policy, action: ActionKind, name: str, expected: RiskClass
) -> None:
    risk, _ = classify(
        action,
        name,
        irreversible_signals=policy.irreversible_signals,
        reversible_signals=policy.reversible_signals,
    )
    assert risk is expected


def test_typing_is_never_a_write(policy: Policy) -> None:
    """Filling a field commits nothing; the submit does. Treating every keystroke as a
    write would make the risk signal fire on every step and mean nothing."""
    risk, _ = classify(
        ActionKind.FILL,
        "Confirm and Open Account",
        irreversible_signals=policy.irreversible_signals,
        reversible_signals=policy.reversible_signals,
    )
    assert risk is RiskClass.READ_ONLY


def test_an_artifact_can_only_declare_stricter_risk(policy: Policy) -> None:
    """Authors are trusted to add caution, not to remove it."""
    stricter, reason = classify(
        ActionKind.CLICK,
        "Search",
        irreversible_signals=policy.irreversible_signals,
        reversible_signals=policy.reversible_signals,
        declared=RiskClass.IRREVERSIBLE_WRITE,
    )
    assert stricter is RiskClass.IRREVERSIBLE_WRITE
    assert "stricter" in reason

    looser, _ = classify(
        ActionKind.CLICK,
        "Confirm and Open Account",
        irreversible_signals=policy.irreversible_signals,
        reversible_signals=policy.reversible_signals,
        declared=RiskClass.READ_ONLY,
    )
    assert looser is RiskClass.IRREVERSIBLE_WRITE, "an artifact must not downgrade risk"


# ------------------------------------------------------------------ fail closed


def test_irreversible_write_is_blocked_without_every_approval(engine: PolicyEngine) -> None:
    decision = engine.check(
        ActionKind.CLICK, control_name="Confirm and Open Account", capability_approved=False
    )
    assert decision.kind is DecisionKind.BLOCK
    assert "capability.status == approved" in decision.required_approval
    assert "--approve" in decision.required_approval


def test_capability_approval_alone_is_not_enough(engine: PolicyEngine) -> None:
    """Two independent gates. An approved artifact still needs the caller to opt in for
    this specific invocation."""
    decision = engine.check(
        ActionKind.CLICK, control_name="Confirm and Open Account", capability_approved=True
    )
    assert decision.kind is DecisionKind.BLOCK
    assert decision.required_approval == ["--approve"]


def test_caller_approval_alone_is_not_enough(policy: Policy) -> None:
    engine = PolicyEngine(policy, caller_approved=True)
    decision = engine.check(
        ActionKind.CLICK, control_name="Confirm and Open Account", capability_approved=False
    )
    assert decision.kind is DecisionKind.BLOCK
    assert decision.required_approval == ["capability.status == approved"]


def test_both_approvals_together_permit_the_write(policy: Policy) -> None:
    engine = PolicyEngine(policy, caller_approved=True)
    decision = engine.check(
        ActionKind.CLICK, control_name="Confirm and Open Account", capability_approved=True
    )
    assert decision.allowed


def test_during_discovery_a_risky_action_escalates_rather_than_running(
    policy: Policy,
) -> None:
    """The model is least trustworthy exactly when it is exploring a UI it does not
    understand. Reusing the escalation path means one mechanism covers two rules."""
    engine = PolicyEngine(policy, caller_approved=True)
    decision = engine.check(
        ActionKind.CLICK,
        control_name="Confirm and Open Account",
        capability_approved=True,
        during_discovery=True,
    )
    assert decision.kind is DecisionKind.ESCALATE


# ------------------------------------------------------------------ network layer


def test_the_network_layer_blocks_off_allowlist_origins(engine: PolicyEngine) -> None:
    """Layer 1 governs what the agent chooses to do; layer 2 governs what the page does
    on its behalf. A redirect to an external IdP, or a beacon injected into a legacy
    app's free-text field, is only stopped by the second."""
    assert engine.allows_request("http://localhost:4000/tenants/meridian/style.css")
    assert not engine.allows_request("https://telemetry.example.com/collect?d=100042")
    assert not engine.allows_request("https://evil.example/x.png")


def test_the_network_layer_permits_paths_the_agent_would_never_navigate_to(
    engine: PolicyEngine,
) -> None:
    """Origin-only on purpose: pages legitimately fetch assets and XHR endpoints outside
    the navigable path allowlist, and blocking those would break the application."""
    assert engine.allows_request("http://localhost:4000/admin/logo.png")
    assert engine.allows_request("data:image/png;base64,AAAA")
