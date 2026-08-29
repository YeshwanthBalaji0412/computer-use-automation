"""`cua replay` - the production execution path.

Composition only. The engine it wires up never imports Playwright or the Anthropic SDK,
which is what the import-linter contracts enforce and what makes "deterministic replay"
a structural property rather than a promise.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from cua.control.intervention import InterventionStore
from cua.control.server import build_app
from cua.control.session import HumanAction, SessionController
from cua.evidence.logger import EventType, EvidenceLogger
from cua.policy.engine import PolicyEngine, load_policy
from cua.policy.redactor import Redactor
from cua.replay.executor import ReplayExecutor
from cua.schema.capability import Capability
from cua.schema.result import EXIT_CODES, ReplayResult
from cua.schema.tenant import TenantProfile, resolve_for_tenant
from cua.surface.web_surface import WebSurface

if TYPE_CHECKING:
    import uvicorn

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
    operator_port: int | None = None,
    goal: str = "",
) -> ReplayResult:
    capability = load_capability(capability_name, capabilities_dir)
    tenant = load_tenant(tenant_id, tenants_dir)
    base_url = base_url_override or tenant.base_url

    # Compose the product-level artifact with this institution's overlay. Pure, and the
    # file on disk is untouched: `git diff` on a capability shows a change to the product
    # automation, `git diff` on a tenant profile shows one institution's specialisation.
    base_capability = capability
    capability = resolve_for_tenant(capability, tenant)

    redactor = Redactor()
    redactor.register_capability(capability, inputs)
    logger = EvidenceLogger(evidence_dir, kind="replay", redactor=redactor)

    if capability is not base_capability:
        overrides = tenant.override_for(base_capability.id)
        logger.event(
            EventType.RUN_STARTED,
            tenant_overlay=tenant.tenant_id,
            steps_added=len(capability.steps) - len(base_capability.steps),
            overridden_steps=sorted(overrides.steps) if overrides else [],
            overridden_outputs=sorted(overrides.outputs) if overrides else [],
        )

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

    # Attended mode. Without an operator port the run is unattended: escalations are
    # still raised and reported honestly, they just terminate instead of parking. One
    # code path, two deployment shapes.
    controller = SessionController() if operator_port else None
    store = InterventionStore(logger.dir) if operator_port else None

    def note_human_action(payload: dict[str, str]) -> None:
        if controller is None:
            return
        controller.record_human_action(
            HumanAction(
                at=datetime.now(UTC),
                kind=payload.get("kind", "action"),
                role=payload.get("role", ""),
                name=payload.get("name", ""),
            )
        )

    surface, pw, browser = await WebSurface.launch(
        headed=headed,
        evidence_dir=logger.steps_dir,
        extra_http_headers=headers,
        allow_request=policy.allows_request,
        on_blocked_request=lambda url: logger.event(
            EventType.POLICY_DECISION, decision="block", rule="network_allowlist", url=url
        ),
        lease_guard=controller.guard() if controller else None,
        on_human_action=note_human_action if controller else None,
    )

    console: tuple[object, object] | None = None
    if controller is not None and store is not None and operator_port:
        console = await _serve_console(
            store=store,
            controller=controller,
            page_provider=lambda: surface.page,
            port=operator_port,
        )
        print(f"operator console -> http://localhost:{operator_port}/", flush=True)

    try:
        executor = ReplayExecutor(
            surface=surface,
            capability=capability,
            policy=policy,
            logger=logger,
            base_url=base_url,
            tenant=tenant_id,
            secrets=secrets_from_env(),
            controller=controller,
            interventions=store,
            operator_base_url=f"http://localhost:{operator_port}" if operator_port else "",
            goal=goal,
        )
        result = await executor.run(inputs)
    finally:
        if controller is not None:
            # Never leave a parked coroutine waiting on a future nobody will complete.
            controller.abandon("run finished")
        if console is not None:
            # Awaited, not fire-and-forget: an un-awaited shutdown leaves the port bound
            # and the next run fails to bind it.
            server, serving = console
            server.should_exit = True  # type: ignore[attr-defined]
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(serving, timeout=5)  # type: ignore[arg-type]
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


async def _serve_console(
    *,
    store: InterventionStore,
    controller: SessionController,
    page_provider: object,
    port: int,
) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    """Run the operator console in the *same* event loop as the browser session.

    Not a subprocess and not a thread: ceding control parks a coroutine on a future that
    only an HTTP request can complete, so the server and the parked automation have to
    share a loop. This is the concrete reason the project uses async Playwright.
    """
    import uvicorn

    app = build_app(store=store, controller=controller, page_provider=page_provider)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())

    deadline = asyncio.get_running_loop().time() + 10
    while not server.started:
        if serving.done():
            serving.result()  # re-raise whatever stopped it (usually a bound port)
            raise RuntimeError("operator console exited during startup")
        if asyncio.get_running_loop().time() > deadline:
            raise RuntimeError("operator console did not start")
        await asyncio.sleep(0.05)
    return server, serving
