"""`cua replay` - the production execution path.

Composition only. The engine it wires up never imports Playwright or the Anthropic SDK,
which is what the import-linter contracts enforce and what makes "deterministic replay"
a structural property rather than a promise.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cua.evidence.logger import EventType, EvidenceLogger
from cua.policy.engine import PolicyEngine, load_policy
from cua.policy.redactor import Redactor
from cua.replay.executor import ReplayExecutor
from cua.schema.capability import Capability
from cua.schema.result import EXIT_CODES, ReplayResult
from cua.schema.tenant import TenantProfile
from cua.surface.web_surface import WebSurface

CAPABILITIES_DIR = Path("capabilities")
TENANTS_DIR = Path("tenants")
EVIDENCE_DIR = Path("evidence")


def load_capability(name: str, directory: Path = CAPABILITIES_DIR) -> Capability:
    """Accepts `id@version` or a bare `id` (newest version wins)."""
    if "@" in name:
        path = directory / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"no capability at {path}")
    else:
        matches = sorted(directory.glob(f"{name}@*.json"))
        if not matches:
            raise FileNotFoundError(f"no capability matching {name!r} in {directory}")
        path = matches[-1]
    return Capability.model_validate_json(path.read_text(encoding="utf-8"))


def load_tenant(tenant_id: str, directory: Path = TENANTS_DIR) -> TenantProfile:
    path = directory / f"{tenant_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no tenant profile at {path}")
    return TenantProfile.model_validate_json(path.read_text(encoding="utf-8"))


def parse_inputs(pairs: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"expected key=value, got {pair!r}")
        key, _, value = pair.partition("=")
        values[key.strip()] = value
    return values


def secrets_from_env() -> dict[str, str]:
    """Resolve `secret_ref` names at execution time.

    The artifact only ever stored the *name*. This is the other half of that decision -
    and the reason a capability file is safe to commit.
    """
    return {
        "corelink.user-id": os.getenv("CUA_DEMO_USERNAME", "operator"),
        "corelink.password": os.getenv("CUA_DEMO_PASSWORD", "demo-pass"),
    }


async def run_replay(
    *,
    capability_name: str,
    inputs: dict[str, str],
    tenant_id: str = "meridian",
    approve: bool = False,
    headed: bool = False,
    fault: str | None = None,
    capabilities_dir: Path = CAPABILITIES_DIR,
    tenants_dir: Path = TENANTS_DIR,
    evidence_dir: Path = EVIDENCE_DIR,
    base_url_override: str | None = None,
    policy_path: Path | None = None,
) -> ReplayResult:
    capability = load_capability(capability_name, capabilities_dir)
    tenant = load_tenant(tenant_id, tenants_dir)
    base_url = base_url_override or tenant.base_url

    redactor = Redactor()
    redactor.register_capability(capability, inputs)
    logger = EvidenceLogger(evidence_dir, kind="replay", redactor=redactor)

    # Policy is data. A different deployment supplies a different file; nothing here
    # lets a capability or a tenant widen what that file permits.
    policy = PolicyEngine(load_policy(policy_path), caller_approved=approve).narrowed(
        allowed_origins=capability.policy.allowed_origins or None,
        allowed_actions=capability.policy.allowed_actions or None,
        max_steps=capability.policy.max_steps,
        max_duration_ms=capability.policy.max_duration_ms,
    )

    # Faults are injected on a header, never in the URL, so the recorded flow is
    # untouched by test scaffolding.
    headers = {"X-CUA-Fault": fault} if fault else {}

    surface, pw, browser = await WebSurface.launch(
        headed=headed,
        evidence_dir=logger.steps_dir,
        extra_http_headers=headers,
        allow_request=policy.allows_request,
        on_blocked_request=lambda url: logger.event(
            EventType.POLICY_DECISION, decision="block", rule="network_allowlist", url=url
        ),
    )

    try:
        executor = ReplayExecutor(
            surface=surface,
            capability=capability,
            policy=policy,
            logger=logger,
            base_url=base_url,
            tenant=tenant_id,
            secrets=secrets_from_env(),
        )
        result = await executor.run(inputs)
    finally:
        await surface.close()
        await browser.close()
        await pw.stop()

    logger.write_manifest(
        capability=capability.qualified_name,
        tenant=tenant_id,
        inputs=inputs,
        status=result.status,
        fault=fault,
    )
    logger.write_report(f"Replay: {capability.qualified_name} ({result.status})")
    return result


def render(result: ReplayResult) -> str:
    lines = [
        f"status     {result.status}",
        f"capability {result.capability}   tenant={result.tenant}",
        f"duration   {result.duration_ms} ms",
    ]
    if result.drift_suspected:
        lines.append("drift      suspected - the screen's shape differs from the recording")

    if result.status == "success":
        lines.append(f"outputs    {json.dumps(result.outputs)}")
    elif result.status == "business_outcome":
        outcome = result.outcome
        lines.append(f"outcome    {outcome.code}: {outcome.message}")
        lines.append("           (a legitimate answer, not a failure)")
    elif result.status == "blocked":
        policy = result.policy
        lines.append(f"rule       {policy.rule}")
        lines.append(f"attempted  {policy.attempted}")
        lines.append(f"needs      {', '.join(policy.required_approval)}")
    elif result.status == "escalated":
        intervention = result.intervention
        lines.append(f"reason     {intervention.reason}")
        lines.append(f"at step    {intervention.step_id}")
    elif result.status == "failed":
        error = result.error
        lines.append(f"error      {error.error_class}")
        lines.append(f"at step    {error.step_id} - {error.step_intent}")
        lines.append(f"expected   {error.expected}")
        lines.append(f"observed   {error.observed}")

    degraded = [t for t in result.steps if t.locator_degraded]
    if degraded:
        lines.append(f"locators   {len(degraded)} step(s) resolved at a lower tier than recorded")
    recovered = [t for t in result.steps if t.recoveries]
    if recovered:
        for trace in recovered:
            for record in trace.recoveries:
                lines.append(f"recovered  {trace.step_id}: {record.recovery_id}")

    if result.evidence:
        lines.append(f"evidence   {result.evidence.run_dir}")
    return "\n".join(lines)


def exit_code(result: ReplayResult) -> int:
    return EXIT_CODES.get(result.status, 1)
