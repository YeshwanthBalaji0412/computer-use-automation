"""`cua eval` - the scenario matrix.

The single most persuasive artifact in the repository: every claim the error taxonomy
makes, demonstrated in one screen, with no API key and no network.

Each row states a *condition* and the *result contract* it must produce. The value is in
how many different answers there are - success, three distinct business outcomes, a
recovery, a policy refusal, an escalation, a hard failure. A system that returned
"failed" for the middle six would pass a naive smoke test and be useless in production.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from cua.app.replay import run_replay


@dataclass(frozen=True)
class Scenario:
    name: str
    inputs: dict[str, str]
    expect_status: str
    #: For business outcomes, the specific code. Asserting only on `status` would let
    #: "not found" and "permission denied" be conflated, which is the compliance problem.
    expect_code: str | None = None
    fault: str | None = None
    tenant: str = "meridian"
    approve: bool = False
    why: str = ""
    expect_recovery: bool = False
    capability: str = "member.savings-balance"


SCENARIOS: list[Scenario] = [
    Scenario(
        "happy-path",
        {"memberId": "100042"},
        "success",
        why="the recorded flow, replayed with no model in the loop",
    ),
    Scenario(
        "different-member",
        {"memberId": "100043"},
        "success",
        why="proves the capability is parameterised, not a recording of one member",
    ),
    Scenario(
        "not-found",
        {"memberId": "999999"},
        "business_outcome",
        expect_code="MEMBER_NOT_FOUND",
        why="a legitimate answer the caller needs - not a crash",
    ),
    Scenario(
        "permission-denied",
        {"memberId": "100777"},
        "business_outcome",
        expect_code="PERMISSION_DENIED",
        why="distinct from not-found; conflating them is a compliance problem",
    ),
    Scenario(
        "invalid-input",
        {"memberId": "ABC"},
        "failed",
        why="rejected by the declared input contract before the browser is even used",
    ),
    Scenario(
        "known-interstitial",
        {"memberId": "100042"},
        "success",
        fault="interstitial",
        expect_recovery=True,
        why="a declared notice is dismissed and the run continues",
    ),
    Scenario(
        "session-expiry",
        {"memberId": "100042"},
        "success",
        fault="expire",
        expect_recovery=True,
        why="the frame bounces to sign-in and the URL never changes; re-authenticate "
        "and restart the flow from its first step",
    ),
    Scenario(
        "unknown-dialog",
        {"memberId": "100042"},
        "escalated",
        fault="unknown-dialog",
        why="an undeclared modal is a question for a human, never a guess",
    ),
    Scenario(
        "app-error",
        {"memberId": "100042"},
        "business_outcome",
        expect_code="APP_ERROR",
        fault="500",
        why="the application's own error page, recognised and reported",
    ),
    Scenario(
        "slow-load",
        {"memberId": "100042"},
        "success",
        fault="slow",
        why="a transient delay is waited out, not failed",
    ),
]


@dataclass
class Row:
    scenario: Scenario
    actual_status: str
    actual_code: str | None
    passed: bool
    detail: str = ""
    recovered: bool = False


@dataclass
class Matrix:
    rows: list[Row] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.rows)

    def render(self) -> str:
        head = f"{'SCENARIO':<22} {'EXPECTED':<28} {'ACTUAL':<28} {'':<4}"
        lines = [head, "-" * len(head)]
        for row in self.rows:
            expected = row.scenario.expect_code or row.scenario.expect_status
            actual = row.actual_code or row.actual_status
            mark = "PASS" if row.passed else "FAIL"
            lines.append(f"{row.scenario.name:<22} {expected:<28} {actual:<28} {mark}")
            if not row.passed and row.detail:
                lines.append(f"{'':<22} -> {row.detail}")
        ok = sum(1 for r in self.rows if r.passed)
        lines += ["", f"{ok}/{len(self.rows)} scenarios passed"]
        return "\n".join(lines)


async def run_matrix(
    *,
    only: str = "",
    base_url: str | None = None,
    capabilities_dir: Path | None = None,
    tenants_dir: Path | None = None,
    evidence_dir: Path | None = None,
    policy_path: Path | None = None,
) -> Matrix:
    from cua.app.replay import CAPABILITIES_DIR, EVIDENCE_DIR, TENANTS_DIR

    matrix = Matrix()
    scenarios = [s for s in SCENARIOS if not only or s.name == only]

    for scenario in scenarios:
        result = await run_replay(
            capability_name=scenario.capability,
            inputs=scenario.inputs,
            tenant_id=scenario.tenant,
            approve=scenario.approve,
            fault=scenario.fault,
            capabilities_dir=capabilities_dir or CAPABILITIES_DIR,
            tenants_dir=tenants_dir or TENANTS_DIR,
            evidence_dir=evidence_dir or EVIDENCE_DIR,
            base_url_override=base_url,
            policy_path=policy_path,
        )

        code = getattr(getattr(result, "outcome", None), "code", None)
        if result.status == "escalated":
            code = str(result.intervention.reason)
        elif result.status == "failed":
            code = str(result.error.error_class)

        recovered = any(t.recoveries for t in result.steps)
        passed = result.status == scenario.expect_status
        if passed and scenario.expect_code:
            passed = code == scenario.expect_code
        if passed and scenario.expect_recovery:
            passed = recovered

        matrix.rows.append(
            Row(
                scenario=scenario,
                actual_status=result.status,
                actual_code=code,
                passed=passed,
                recovered=recovered,
                detail=_detail(result),
            )
        )
    return matrix


def _detail(result: object) -> str:
    error = getattr(result, "error", None)
    if error is not None:
        return f"{error.error_class} at {error.step_id}: expected {error.expected}"
    intervention = getattr(result, "intervention", None)
    if intervention is not None:
        return f"escalated: {intervention.reason} at {intervention.step_id}"
    return ""
