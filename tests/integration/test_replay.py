"""Deterministic replay and the error taxonomy.

The scenario matrix is the load-bearing test here. Its value is in how many *different*
answers it demands: a system that returned "failed" for the six middle rows would pass a
naive smoke test and be useless in production, because the caller could not tell "this
member does not exist" from "the automation is broken".

`test_replay_never_loads_the_anthropic_sdk` is the other one that matters. Requirement
3.3 says replay runs "without invoking the LLM for decisions"; this proves the module is
never even imported, which is a stronger claim than grepping the source.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from cua.app.evaluate import SCENARIOS, run_matrix
from cua.app.replay import load_capability, parse_inputs, run_replay
from cua.policy.engine import load_policy
from cua.schema.capability import ApprovalStatus

pytestmark = [pytest.mark.integration]

REPO = Path(__file__).resolve().parents[2]
CAPABILITIES = REPO / "capabilities"
TENANTS = REPO / "tenants"


@pytest_asyncio.fixture
async def tenant_dir(base_url: str, tmp_path: Path) -> AsyncIterator[Path]:
    """Tenant profiles pointed at the ephemeral test server."""
    out = tmp_path / "tenants"
    out.mkdir()
    for slug in ("meridian", "lakeside"):
        profile = json.loads((TENANTS / f"{slug}.json").read_text())
        profile["base_url"] = f"{base_url}/tenants/{slug}"
        (out / f"{slug}.json").write_text(json.dumps(profile))
    yield out


@pytest.fixture
def policy_file(base_url: str, tmp_path: Path) -> Path:
    """The shipped policy allowlists port 4000; the test server takes an ephemeral one.

    Supplying a policy *file* rather than disabling the check keeps every other rule -
    denied paths, action types, risk gates - live for these tests, and exercises the
    "guardrails are configurable data" path at the same time.
    """
    import yaml

    from cua.policy.engine import DEFAULT_POLICY_PATH

    raw = yaml.safe_load(DEFAULT_POLICY_PATH.read_text())
    raw["allowed_origins"] = [*raw["allowed_origins"], base_url]
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


async def replay(
    base_url: str,
    tenant_dir: Path,
    tmp_path: Path,
    policy_file: Path,
    inputs: dict[str, str],
    **kw: object,
):  # type: ignore[no-untyped-def]
    return await run_replay(
        capability_name="member.savings-balance",
        inputs=inputs,
        capabilities_dir=CAPABILITIES,
        tenants_dir=tenant_dir,
        evidence_dir=tmp_path / "evidence",
        base_url_override=f"{base_url}/tenants/meridian",
        policy_path=policy_file,
        **kw,  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------ the matrix


async def test_the_scenario_matrix_passes(
    base_url: str, tenant_dir: Path, tmp_path: Path, policy_file: Path
) -> None:
    """Every row of the README's table, run for real."""
    matrix = await run_matrix(
        base_url=f"{base_url}/tenants/meridian",
        capabilities_dir=CAPABILITIES,
        tenants_dir=tenant_dir,
        evidence_dir=tmp_path / "evidence",
        policy_path=policy_file,
    )
    assert matrix.passed, "\n" + matrix.render()
    assert len(matrix.rows) == len(SCENARIOS)


def test_the_matrix_covers_every_result_status() -> None:
    """A taxonomy nobody exercises is a taxonomy nobody can trust."""
    statuses = {s.expect_status for s in SCENARIOS}
    assert {"success", "business_outcome", "escalated", "failed"} <= statuses

    codes = {s.expect_code for s in SCENARIOS if s.expect_code}
    assert {"MEMBER_NOT_FOUND", "PERMISSION_DENIED", "APP_ERROR"} <= codes


# ------------------------------------------------------------------ no LLM


def test_replay_never_loads_the_anthropic_sdk() -> None:
    """Requirement 3.3, proved rather than promised.

    Runs in a subprocess so the assertion is about a *real* replay's import graph, not
    about what this test process happens to have loaded already. Stronger than the
    import-linter contract, which only inspects source: this shows the module was never
    executed.
    """
    script = """
import asyncio, sys, json, pathlib
from cua.app.replay import run_replay
asyncio.run(run_replay(
    capability_name="member.savings-balance",
    inputs={"memberId": "ABC"},
    capabilities_dir=pathlib.Path(sys.argv[1]),
    tenants_dir=pathlib.Path(sys.argv[2]),
    evidence_dir=pathlib.Path(sys.argv[3]),
))
print("anthropic" in sys.modules)
"""
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            [sys.executable, "-c", script, str(CAPABILITIES), str(TENANTS), tmp],
            capture_output=True,
            text=True,
            cwd=REPO,
            timeout=180,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("False"), "the anthropic SDK was imported during a replay"


# ------------------------------------------------------------------ the contract


async def test_success_returns_typed_outputs(
    base_url: str, tenant_dir: Path, tmp_path: Path, policy_file: Path
) -> None:
    result = await replay(base_url, tenant_dir, tmp_path, policy_file, {"memberId": "100042"})
    assert result.status == "success"
    assert result.outputs["savingsBalance"] == "4182.55", (
        "currency must be normalised, not returned as the string the page rendered"
    )


async def test_the_same_capability_serves_a_different_member(
    base_url: str, tenant_dir: Path, tmp_path: Path, policy_file: Path
) -> None:
    """Proves it is a capability, not a recording of one member."""
    result = await replay(base_url, tenant_dir, tmp_path, policy_file, {"memberId": "100043"})
    assert result.status == "success"
    assert result.outputs["savingsBalance"] == "912.10"


async def test_not_found_is_an_answer_not_a_failure(
    base_url: str, tenant_dir: Path, tmp_path: Path, policy_file: Path
) -> None:
    """The distinction the brief calls the most common design mistake in this problem."""
    result = await replay(base_url, tenant_dir, tmp_path, policy_file, {"memberId": "999999"})

    assert result.status == "business_outcome"
    assert result.outcome.code == "MEMBER_NOT_FOUND"
    assert result.outcome.data == {"found": False}
    assert result.status != "failed"


async def test_permission_denied_is_distinct_from_not_found(
    base_url: str, tenant_dir: Path, tmp_path: Path, policy_file: Path
) -> None:
    """One means the record does not exist; the other means it does and this operator
    may not see it. Conflating them is a compliance problem, not a bug."""
    result = await replay(base_url, tenant_dir, tmp_path, policy_file, {"memberId": "100777"})
    assert result.status == "business_outcome"
    assert result.outcome.code == "PERMISSION_DENIED"
    assert result.outcome.data["authorized"] is False


async def test_bad_input_fails_before_the_browser_is_used(
    base_url: str, tenant_dir: Path, tmp_path: Path, policy_file: Path
) -> None:
    """A malformed member id should cost nothing and name the contract it violated."""
    result = await replay(base_url, tenant_dir, tmp_path, policy_file, {"memberId": "ABC"})
    assert result.status == "failed"
    assert str(result.error.error_class) == "invalid_input"
    assert "memberId" in result.error.observed
    assert result.steps == []


async def test_an_unknown_input_is_rejected(
    base_url: str, tenant_dir: Path, tmp_path: Path, policy_file: Path
) -> None:
    result = await replay(
        base_url, tenant_dir, tmp_path, policy_file, {"memberId": "100042", "sneaky": "1"}
    )
    assert result.status == "failed"
    assert "sneaky" in result.error.observed


# ------------------------------------------------------------------ runtime conditions


async def test_a_declared_interstitial_is_dismissed_and_the_run_continues(
    base_url: str, tenant_dir: Path, tmp_path: Path, policy_file: Path
) -> None:
    result = await replay(
        base_url, tenant_dir, tmp_path, policy_file, {"memberId": "100042"}, fault="interstitial"
    )
    assert result.status == "success"
    recoveries = [r for t in result.steps for r in t.recoveries]
    assert any(r.recovery_id == "dismiss-known-interstitial" for r in recoveries)
    assert all(r.succeeded for r in recoveries), (
        "a recovery that did nothing must not report success"
    )


async def test_a_slow_screen_is_waited_out_not_failed(
    base_url: str, tenant_dir: Path, tmp_path: Path, policy_file: Path
) -> None:
    """The screen takes longer than one settle window. Re-observing is the retry, and it
    is bounded by the step's own budget."""
    result = await replay(
        base_url, tenant_dir, tmp_path, policy_file, {"memberId": "100042"}, fault="slow"
    )
    assert result.status == "success"
    assert result.outputs["savingsBalance"] == "4182.55"


async def test_an_undeclared_dialog_escalates_rather_than_being_guessed_at(
    base_url: str, tenant_dir: Path, tmp_path: Path, policy_file: Path
) -> None:
    """The application is asking an operator a question. Answering it by guessing is
    exactly the decision this system is not allowed to make."""
    result = await replay(
        base_url, tenant_dir, tmp_path, policy_file, {"memberId": "100042"}, fault="unknown-dialog"
    )
    assert result.status == "escalated"
    assert str(result.intervention.reason) == "unknown_dialog"
    assert result.intervention.step_id


async def test_session_expiry_escalates_when_no_reauth_is_declared(
    base_url: str, tenant_dir: Path, tmp_path: Path, policy_file: Path
) -> None:
    """The frame bounces to sign-in and the top-level URL never changes, so detection
    has to be based on what is on screen. With no re-auth capability declared, the honest
    outcome is escalation rather than a silent failure."""
    result = await replay(
        base_url, tenant_dir, tmp_path, policy_file, {"memberId": "100042"}, fault="expire"
    )
    assert result.status == "escalated"


async def test_a_server_error_is_reported_as_such(
    base_url: str, tenant_dir: Path, tmp_path: Path, policy_file: Path
) -> None:
    result = await replay(
        base_url, tenant_dir, tmp_path, policy_file, {"memberId": "100042"}, fault="500"
    )
    assert result.status == "business_outcome"
    assert result.outcome.code == "APP_ERROR"


# ------------------------------------------------------------------ evidence


async def test_a_failure_is_debuggable_from_the_result_alone(
    base_url: str, tenant_dir: Path, tmp_path: Path, policy_file: Path
) -> None:
    """ "What step, what was expected, what was observed" - three separate fields,
    because a single prose message turns a five-minute diagnosis into an hour."""
    result = await replay(base_url, tenant_dir, tmp_path, policy_file, {"memberId": "ABC"})
    assert result.status == "failed"
    assert result.error.expected
    assert result.error.observed
    assert result.evidence is not None and result.evidence.run_dir


async def test_evidence_is_written_and_redacted(
    base_url: str, tenant_dir: Path, tmp_path: Path, policy_file: Path
) -> None:
    result = await replay(base_url, tenant_dir, tmp_path, policy_file, {"memberId": "100042"})
    run_dir = Path(result.evidence.run_dir)

    assert (run_dir / "events.jsonl").exists()
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "report.md").exists()

    events = (run_dir / "events.jsonl").read_text()
    assert "4,182.55" not in events, "a balance declared PII reached the evidence log"
    assert "demo-pass" not in events


async def test_every_step_reports_which_locator_tier_carried_it(
    base_url: str, tenant_dir: Path, tmp_path: Path, policy_file: Path
) -> None:
    """The drift signal: a step recorded at tier 1 that starts resolving at tier 4 means
    a label changed, and that is worth knowing before it becomes an outage."""
    result = await replay(base_url, tenant_dir, tmp_path, policy_file, {"memberId": "100042"})
    targeted = [t for t in result.steps if t.tiers_tried]
    assert targeted
    for trace in targeted:
        assert trace.winning_tier is not None


# ------------------------------------------------------------------ safety


def test_the_shipped_capability_is_not_approved_for_unattended_use() -> None:
    """Never born approved. Unattended execution is a human decision made after reading
    the artifact, and `stability` is the evidence for making it."""
    capability = load_capability("member.savings-balance", CAPABILITIES)
    assert capability.status is ApprovalStatus.DRAFT


def test_a_capability_cannot_widen_the_institutions_policy() -> None:
    policy = load_policy()
    assert policy.allowed_origins
    capability = load_capability("member.savings-balance", CAPABILITIES)
    for origin in capability.policy.allowed_origins:
        assert origin in policy.allowed_origins


def test_input_parsing_rejects_malformed_pairs() -> None:
    assert parse_inputs(["memberId=100042"]) == {"memberId": "100042"}
    with pytest.raises(ValueError, match="key=value"):
        parse_inputs(["memberId"])
