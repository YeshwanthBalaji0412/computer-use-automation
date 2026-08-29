"""One artifact, two institutions.

The brief's §3.7: reuse across tenants running the same vendor product, "rather than
re-recorded per tenant". This is that claim, run against two live variants of the same
app that differ in the ways which actually break automation - a renamed button, a
renamed field, a mandatory extra step, and the same data in a different structure.

`test_a_meridian_recording_replays_on_lakeside` is the one that matters. Everything else
here supports it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from cua.app.replay import run_replay
from cua.app.verify import sweep
from cua.policy.engine import DEFAULT_POLICY_PATH

pytestmark = [pytest.mark.integration]

REPO = Path(__file__).resolve().parents[2]
CAPABILITIES = REPO / "capabilities"
TENANTS = REPO / "tenants"


@pytest.fixture
def env(base_url: str, tmp_path: Path) -> dict[str, Path]:
    tenants = tmp_path / "tenants"
    tenants.mkdir()
    for slug in ("meridian", "lakeside"):
        profile = json.loads((TENANTS / f"{slug}.json").read_text())
        profile["base_url"] = f"{base_url}/tenants/{slug}"
        (tenants / f"{slug}.json").write_text(json.dumps(profile))

    raw = yaml.safe_load(DEFAULT_POLICY_PATH.read_text())
    raw["allowed_origins"] = [*raw["allowed_origins"], base_url]
    policy = tmp_path / "policy.yaml"
    policy.write_text(yaml.safe_dump(raw))
    return {"tenants": tenants, "policy": policy, "evidence": tmp_path / "evidence"}


async def replay_on(tenant: str, env: dict[str, Path], base_url: str):  # type: ignore[no-untyped-def]
    return await run_replay(
        capability_name="member.savings-balance",
        inputs={"memberId": "100042"},
        tenant_id=tenant,
        capabilities_dir=CAPABILITIES,
        tenants_dir=env["tenants"],
        evidence_dir=env["evidence"],
        base_url_override=f"{base_url}/tenants/{tenant}",
        policy_path=env["policy"],
    )


async def test_a_meridian_recording_replays_on_lakeside(
    base_url: str, env: dict[str, Path]
) -> None:
    """Recorded once, against Meridian. Replayed unmodified at an institution whose
    search button says "Find Member", whose id field says "Member Number", which demands
    a branch selection first, and which renders balances as a definition list."""
    meridian = await replay_on("meridian", env, base_url)
    lakeside = await replay_on("lakeside", env, base_url)

    assert meridian.status == "success", getattr(meridian, "error", None)
    assert lakeside.status == "success", getattr(lakeside, "error", None)
    assert meridian.outputs["savingsBalance"] == lakeside.outputs["savingsBalance"]


async def test_the_overlay_adds_a_step_lakeside_requires(
    base_url: str, env: dict[str, Path]
) -> None:
    meridian = await replay_on("meridian", env, base_url)
    lakeside = await replay_on("lakeside", env, base_url)

    assert len(lakeside.steps) == len(meridian.steps) + 1
    assert any("branch" in t.step_id for t in lakeside.steps)


async def test_lakeside_reports_which_steps_leaned_on_the_overlay(
    base_url: str, env: dict[str, Path]
) -> None:
    """It did not merely work - it said *how*. A step that resolves below its recorded
    tier is the earliest warning that an institution's UI has moved, and it is worth as
    much as the success itself."""
    lakeside = await replay_on("lakeside", env, base_url)
    meridian = await replay_on("meridian", env, base_url)

    assert any(t.locator_degraded for t in lakeside.steps)
    assert not any(t.locator_degraded for t in meridian.steps)


async def test_the_conformance_sweep_finds_both_tenants_serviceable(
    base_url: str, env: dict[str, Path]
) -> None:
    """The nightly check: resolve every step at every tenant, execute only the read-only
    ones, and stop at the write boundary. Unresolved steps are what will fail next."""
    results = await sweep(
        capability_name="member.savings-balance",
        tenant_ids=["meridian", "lakeside"],
        capabilities_dir=CAPABILITIES,
        tenants_dir=env["tenants"],
        policy_path=env["policy"],
    )

    assert {r.tenant_id for r in results} == {"meridian", "lakeside"}
    for report in results:
        assert not report.error, report.error
        assert report.unresolved == 0, (
            f"{report.tenant_id}: {[s.step_id for s in report.steps if s.resolved_tier is None]}"
        )

    lakeside = next(r for r in results if r.tenant_id == "lakeside")
    meridian = next(r for r in results if r.tenant_id == "meridian")
    assert lakeside.degraded > 0, "the overlay should be visible in the telemetry"
    assert meridian.degraded == 0, "the tenant it was recorded on should be clean"
