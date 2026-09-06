"""`cua eval` - the scenario matrix.

The single most persuasive artifact in the repository: every claim the error taxonomy
makes, demonstrated in one screen, with no API key and no network.

Each row states a *condition* and the *result contract* it must produce. The value is in
how many different answers there are - success, three distinct business outcomes, a
recovery, a policy refusal, an escalation, a hard failure. A system that returned
"failed" for the middle six would pass a naive smoke test and be useless in production.
"""

from __future__ import annotations

import uuid
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
        "failed",
        expect_code="app_error",
        fault="500",
        why="the application's own error page: declared in the contract so a caller "
        "knows it can happen, but returned as a failure because a 500 is not an answer",
    ),
    Scenario(
        "slow-load",
        {"memberId": "100042"},
        "success",
        fault="slow",
        why="a transient delay is waited out, not failed",
    ),
    # ---------------------------------------------------------------- the write flow
    #
    # `{run}` in an input is replaced with a token unique to this invocation. That is not
    # test scaffolding, it is the constraint a non-idempotent capability puts on anyone
    # who wants to exercise it repeatedly: you cannot reset a core banking system, so a
    # regression suite that opens accounts has to open a *different* one each time. The
    # alternative - a reset endpoint - would be a thing this app has and a real one never
    # will, and building the matrix around it would prove the wrong thing.
    #
    # `write-duplicate` is the deliberate exception: it targets a nickname seeded into the
    # fixtures, so the collision is guaranteed rather than dependent on run order.
    Scenario(
        "write-blocked",
        {"memberId": "100042", "nickname": "HOLIDAY FUND"},
        "blocked",
        capability="member.open-subaccount",
        why="an irreversible write with no caller approval is refused before it acts",
    ),
    Scenario(
        "write-success",
        {"memberId": "100042", "nickname": "EVAL {run}"},
        "success",
        capability="member.open-subaccount",
        approve=True,
        why="an approved capability plus an explicit caller approval really does open the "
        "account, and returns the confirmation number as a typed output",
    ),
    Scenario(
        "write-duplicate",
        {"memberId": "100042", "nickname": "VACATION FUND"},
        "business_outcome",
        expect_code="DUPLICATE_RECORD",
        capability="member.open-subaccount",
        approve=True,
        why="the application refuses at the review screen, before committing - which is "
        "what makes a non-idempotent capability safe for a caller to retry",
    ),
    Scenario(
        "write-ambiguous",
        {"memberId": "100042", "nickname": "AMBIG {run}"},
        "escalated",
        expect_code="ambiguous_write_outcome",
        capability="member.open-subaccount",
        approve=True,
        fault="write-timeout",
        why="the confirm committed and then lost its acknowledgement. Nothing on screen "
        "distinguishes that from a write that never landed, so retrying could open the "
        "account twice and the only safe answer is to stop and ask",
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

    # One token per matrix run, so a scenario that opens an account opens a new one each
    # time. See the note on the write-flow scenarios: this is what replaces a reset that
    # no real system would offer.
    run_token = uuid.uuid4().hex[:6].upper()

    for scenario in scenarios:
        inputs = {k: v.replace("{run}", run_token) for k, v in scenario.inputs.items()}
        result = await run_replay(
            capability_name=scenario.capability,
            inputs=inputs,
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
