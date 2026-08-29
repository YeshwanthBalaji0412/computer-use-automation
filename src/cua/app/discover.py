"""`cua discover` - wire the pieces together and write the artifact.

Kept out of cli.py so the CLI stays a thin composition root. This is the only place that
decides *which* LLM client to construct, which is what makes `--mock` a one-line
substitution rather than a parallel code path.
"""

from __future__ import annotations

import json
from pathlib import Path

from cua.app.replay import secrets_from_env
from cua.discovery.agent import DiscoveryAgent, StopReason
from cua.discovery.compiler import compile_capability
from cua.discovery.llm import LLMClient, MockLLM, OpenAIClient
from cua.discovery.prompt import PROMPT_VERSION
from cua.evidence.logger import EventType, EvidenceLogger
from cua.policy.engine import PolicyEngine, load_policy
from cua.policy.redactor import Redactor
from cua.surface.web_surface import WebSurface

CAPABILITIES_DIR = Path("capabilities")
EVIDENCE_DIR = Path("evidence")
FIXTURE_PATH = Path("evidence/fixtures/discovery-transcript.json")


async def run_discover(
    *,
    goal: str,
    target: str,
    tenant: str,
    mock: bool,
    headed: bool = False,
    fixture: Path | None = None,
    capabilities_dir: Path = CAPABILITIES_DIR,
    evidence_dir: Path = EVIDENCE_DIR,
) -> int:
    llm: LLMClient
    if mock:
        path = fixture or FIXTURE_PATH
        if not path.exists():
            print(
                f"no recorded transcript at {path}.\n"
                "Record one first with a live run, or point --fixture at one."
            )
            return 2
        llm = MockLLM.from_fixture(path)
    else:
        llm = OpenAIClient()

    redactor = Redactor()
    logger = EvidenceLogger(evidence_dir, kind="discovery", redactor=redactor)

    policy_engine = PolicyEngine(load_policy())
    surface, pw, browser = await WebSurface.launch(
        headed=headed,
        evidence_dir=logger.steps_dir,
        allow_request=policy_engine.allows_request,
        on_blocked_request=lambda url: logger.event(
            EventType.POLICY_DECISION, decision="block", rule="network_allowlist", url=url
        ),
    )

    try:
        agent = DiscoveryAgent(
            surface=surface,
            llm=llm,
            policy=policy_engine,
            logger=logger,
            secrets=secrets_from_env(),
        )
        result = await agent.run(goal=goal, target=target, tenant=tenant)
    finally:
        await surface.close()
        await browser.close()
        await pw.stop()

    print(f"\nrun      {logger.run_id}")
    print(f"stopped  {result.stop_reason}")
    print(
        f"actions  {len(result.recorder.effective_actions)} effective "
        f"of {len(result.recorder.actions)} attempted"
    )
    if result.input_tokens:
        print(f"tokens   {result.input_tokens} in / {result.output_tokens} out")

    if result.stop_reason != StopReason.FINISHED:
        logger.write_manifest(goal=goal, tenant=tenant, stop_reason=result.stop_reason)
        logger.write_report("Discovery run (incomplete)")
        print(f"\nno artifact written. evidence: {logger.dir}")
        return 1

    base_url = _base_url(target)
    capability = compile_capability(
        result.recorder,
        capability_id=result.capability_id or "unnamed.capability",
        description=result.description,
        display_name=result.capability_id.replace(".", " ").title(),
        goal=goal,
        tenant=tenant,
        base_url=base_url,
        model=llm.model_name,
        prompt_version=PROMPT_VERSION,
        run_id=logger.run_id,
    )

    # A capability with nothing to verify is worse than no capability: replay would
    # report success for any screen it happened to land on. Refusing here, loudly, is
    # what turns a silently degraded recording into an obvious one - which is exactly
    # how a stale --mock fixture was caught producing a five-step artifact that
    # "succeeded" without ever opening a member.
    if capability.success_condition is None and not capability.outputs:
        logger.write_manifest(goal=goal, tenant=tenant, stop_reason="unverifiable")
        logger.write_report("Discovery run (unverifiable)")
        print(
            "\nno artifact written: the recorded flow declares no success condition and\n"
            "no outputs, so replay would have no way to tell whether it worked.\n"
            "The model must call `assert_state` on the screen that proves the goal was\n"
            "reached, or `extract` a value from it."
        )
        print(f"evidence {logger.dir}")
        return 1

    capabilities_dir.mkdir(parents=True, exist_ok=True)
    artifact = capabilities_dir / f"{capability.id}@{capability.version}.json"
    artifact.write_text(
        json.dumps(capability.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # The raw transcript is saved *separately* from the artifact, and re-usable as a
    # --mock fixture. Keeping them apart is the point: the transcript is evidence of how
    # the flow was found; the artifact is the contract, compiled from what actually ran.
    MockLLM.write_fixture(logger.dir / "transcript.json", result.turns, model=llm.model_name)

    logger.write_manifest(
        goal=goal,
        tenant=tenant,
        stop_reason=result.stop_reason,
        capability=capability.qualified_name,
        artifact=str(artifact),
    )
    logger.write_report(f"Discovery: {capability.qualified_name}")

    print(f"\nartifact {artifact}")
    print(
        f"  {len(capability.steps)} steps, "
        f"{len(capability.inputs)} input(s), {len(capability.outputs)} output(s), "
        f"{len(capability.known_outcomes)} known outcome(s)"
    )
    print(f"  status={capability.status} risk={capability.risk_class}")
    print(f"evidence {logger.dir}")
    return 0


def _base_url(target: str) -> str:
    """Everything up to and including the tenant mount.

    Used to canonicalise recorded URLs into `{{base_url}}/...` so the artifact describes
    the vendor product rather than one tenant's deployment of it.
    """
    from urllib.parse import urlparse

    parsed = urlparse(target)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "tenants":
        return f"{parsed.scheme}://{parsed.netloc}/tenants/{parts[1]}"
    return f"{parsed.scheme}://{parsed.netloc}"
