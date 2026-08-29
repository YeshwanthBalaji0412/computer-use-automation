"""`cua verify` - the conformance sweep.

Answers the question the multi-tenant design has to answer eventually: *how do you know,
before a member is on the phone, that this capability still works at all 100
institutions?*

Safe by construction, but not by refusing to act - by refusing to *write*. The sweep
walks the recorded flow executing only steps the artifact classifies `read_only`, and
stops at the first one that changes anything. A version that clicked nothing could only
ever verify the first screen, which is the shallowest possible check and would miss
exactly the drift that matters.
What it reports is not pass/fail but **which tier each step resolved at**, per tenant,
because that is the leading indicator: a step recorded at tier 1 that starts resolving at
tier 4 means somebody relabelled a button, and you want to know that on the nightly sweep
rather than from a failed transaction.

Deliberately not built: the dashboard, the alerting, and the automatic override-proposal
workflow that would sit on top of this in production. The signal is the hard part; the
plumbing around it is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from cua.app.replay import load_capability, load_tenant
from cua.schema.capability import ActionKind, Capability, RiskClass, Step
from cua.schema.locator import Tier

if TYPE_CHECKING:
    from cua.surface.base import ActionType
from cua.schema.tenant import resolve_for_tenant


@dataclass
class StepConformance:
    step_id: str
    intent: str
    recorded_tier: Tier | None
    resolved_tier: Tier | None
    outcome: str
    #: The sweep stopped before this step because continuing would have written.
    not_reached: bool = False

    @property
    def degraded(self) -> bool:
        return (
            self.resolved_tier is not None
            and self.recorded_tier is not None
            and self.resolved_tier > self.recorded_tier
        )


@dataclass
class TenantConformance:
    tenant_id: str
    steps: list[StepConformance] = field(default_factory=list)
    error: str = ""

    @property
    def unresolved(self) -> int:
        """Steps that were reached and could not be found. These are the ones that will
        fail; a step the sweep stopped short of is not evidence of anything."""
        return sum(1 for s in self.steps if s.resolved_tier is None and not s.not_reached)

    @property
    def not_reached(self) -> int:
        return sum(1 for s in self.steps if s.not_reached)

    @property
    def degraded(self) -> int:
        return sum(1 for s in self.steps if s.degraded)


def render(results: list[TenantConformance], capability: str) -> str:
    lines = [f"conformance: {capability}", ""]
    head = f"{'TENANT':<12} {'STEPS':>6} {'DEGRADED':>9} {'UNRESOLVED':>11}  NOTES"
    lines += [head, "-" * len(head)]
    for result in results:
        notes = result.error or ", ".join(
            f"{s.step_id}:t{int(s.recorded_tier or 0)}->t{int(s.resolved_tier or 0)}"
            for s in result.steps
            if s.degraded or (s.resolved_tier is None and not s.not_reached)
        )
        if not notes and result.not_reached:
            notes = f"stopped before {result.not_reached} write step(s)"
        lines.append(
            f"{result.tenant_id:<12} {len(result.steps):>6} {result.degraded:>9} "
            f"{result.unresolved:>11}  {notes or 'clean'}"
        )
    lines += [
        "",
        "A degraded step still works - a better-ranked strategy stopped matching, which",
        "is the earliest warning that this tenant's UI has moved. Unresolved steps are",
        "the ones that will fail the next time somebody calls this capability.",
    ]
    return "\n".join(lines)


def tenants_in(directory: Path) -> list[str]:
    return sorted(p.stem for p in directory.glob("*.json"))


async def sweep(
    *,
    capability_name: str,
    tenant_ids: list[str],
    capabilities_dir: Path,
    tenants_dir: Path,
    base_url_override: str | None = None,
    policy_path: Path | None = None,
    record_stability: bool = False,
) -> list[TenantConformance]:
    """Walk each tenant's screens and resolve every step, without acting on anything.

    `record_stability` writes the result back into the artifact. Off by default so a test
    sweeping the committed capabilities directory does not mutate what it is measuring;
    the CLI turns it on, because the nightly sweep is exactly the controlled measurement
    an approval decision should rest on.
    """
    import contextlib

    from cua.app.replay import secrets_from_env
    from cua.locator.generate import bind
    from cua.locator.match import resolve as match_resolve
    from cua.policy.engine import PolicyEngine, load_policy
    from cua.schema.locator import ResolveOutcome
    from cua.surface.base import Action, ActionType
    from cua.surface.web_surface import WebSurface

    secrets = secrets_from_env()

    base = load_capability(capability_name, capabilities_dir)
    policy = PolicyEngine(load_policy(policy_path))
    results: list[TenantConformance] = []

    for tenant_id in tenant_ids:
        report = TenantConformance(tenant_id=tenant_id)
        try:
            profile = load_tenant(tenant_id, tenants_dir)
        except FileNotFoundError as exc:
            report.error = str(exc)
            results.append(report)
            continue

        capability = resolve_for_tenant(base, profile)
        root = (base_url_override or profile.base_url).rstrip("/")

        surface, pw, browser = await WebSurface.launch(allow_request=policy.allows_request)
        try:
            entry = capability.target.entry_url_pattern.replace("{{base_url}}", root)
            await surface.act(Action(type=ActionType.NAVIGATE, url=entry))
            observation = await surface.observe()

            # Example values only, so the sweep exercises parameterised locators without
            # touching a real record. It never clicks, so no state can change.
            values = {p.name: (p.example or "") for p in capability.inputs}

            stopped = False
            for step in capability.steps:
                target = capability.effective_locator(step)
                if target is None:
                    continue

                if stopped or step.risk is not RiskClass.READ_ONLY:
                    # The write boundary. Everything from here on is unverified rather
                    # than broken, and saying so is the difference between a useful
                    # report and a misleading one.
                    stopped = True
                    report.steps.append(
                        StepConformance(
                            step_id=step.id,
                            intent=step.intent,
                            recorded_tier=target.best_tier,
                            resolved_tier=None,
                            outcome="not_reached",
                            not_reached=True,
                        )
                    )
                    continue

                bound = bind(target, values)
                resolution = match_resolve(bound, observation)
                report.steps.append(
                    StepConformance(
                        step_id=step.id,
                        intent=step.intent,
                        recorded_tier=target.best_tier,
                        resolved_tier=resolution.winning_tier,
                        outcome=str(resolution.outcome),
                    )
                )
                if resolution.outcome is not ResolveOutcome.RESOLVED:
                    stopped = True
                    continue

                if step.action is ActionKind.EXTRACT:
                    continue  # reading a value changes nothing; resolution was the check

                await surface.act(
                    Action(
                        type=_surface_action(step.action),
                        ref=resolution.ref,
                        text=_value_for(step, values, secrets),
                        value=_value_for(step, values, secrets),
                    )
                )
                observation = await surface.observe()
        except Exception as exc:  # a broken tenant is a finding, not a crash
            report.error = f"{type(exc).__name__}: {exc}"
        finally:
            with contextlib.suppress(Exception):
                await surface.close()
                await browser.close()
                await pw.stop()

        results.append(report)

    if record_stability:
        _record_stability(capability_name, results, capabilities_dir)
    return results


def _record_stability(
    capability_name: str, results: list[TenantConformance], capabilities_dir: Path
) -> None:
    """Write what the sweep measured back into the artifact.

    `Stability` is the evidence an approval rests on. Without this it stayed at 0/0
    forever, which made `cua approve` a rubber stamp with a number next to it - the exact
    failure mode of a governance control that nobody feeds.

    The tier histogram is the part worth keeping over time. A capability that used to
    resolve everything at tier 1 and now leans on tier 4 has not broken, but it is on its
    way there, and that trend is invisible from any single run.
    """
    from datetime import UTC, datetime

    path = _artifact_path(capability_name, capabilities_dir)
    if path is None:
        return

    capability = Capability.model_validate_json(path.read_text(encoding="utf-8"))
    stability = capability.stability

    for report in results:
        stability.replays += 1
        if not report.error and not report.unresolved:
            stability.successes += 1
        for step in report.steps:
            if step.resolved_tier is not None:
                key = f"tier{int(step.resolved_tier)}"
                stability.locator_tier_histogram[key] = (
                    stability.locator_tier_histogram.get(key, 0) + 1
                )

    stability.last_verified_at = datetime.now(UTC)
    path.write_text(capability.model_dump_json(indent=2), encoding="utf-8")


def _artifact_path(capability_name: str, capabilities_dir: Path) -> Path | None:
    if "@" in capability_name:
        path = capabilities_dir / f"{capability_name}.json"
        return path if path.exists() else None
    matches = sorted(capabilities_dir.glob(f"{capability_name}@*.json"))
    return matches[-1] if matches else None


def _surface_action(kind: ActionKind) -> ActionType:
    from cua.surface.base import ActionType

    return {
        ActionKind.NAVIGATE: ActionType.NAVIGATE,
        ActionKind.CLICK: ActionType.CLICK,
        ActionKind.FILL: ActionType.FILL,
        ActionKind.SELECT: ActionType.SELECT,
        ActionKind.PRESS: ActionType.PRESS,
    }[kind]


def _value_for(step: Step, values: dict[str, str], secrets: dict[str, str]) -> str | None:
    if step.value is None:
        return None
    if step.value.param:
        return values.get(step.value.param, "")
    if step.value.secret_ref:
        return secrets.get(step.value.secret_ref, "")
    return step.value.literal
