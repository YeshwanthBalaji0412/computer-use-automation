"""Tenant profiles and overlays.

An artifact targets a *vendor product*; a tenant is one institution's deployment of that
product, branded and configured differently. Roughly 100 tenants running ~20 apps each is
2,000 instances but only ~20 products - so the unit of authoring has to be the product,
and a tenant has to be a thin, reviewable diff on top of it.

Composition happens at replay time and never mutates the artifact on disk. That matters
for review: `git diff` on a capability shows a change to the *product* automation, and
`git diff` on a tenant profile shows one institution's specialisation. Merging them into
one file would make both unreviewable, and would mean re-recording a flow every time an
institution renamed a button.

Two constraints on what an overlay may do
-----------------------------------------
**Strategies are prepended, never replaced.** A tenant adds a way to find a control; the
recorded ladder stays underneath as a fallback. So a tenant that quietly reverts to the
standard labelling keeps working, and - because the winning tier is reported - you can
see from the telemetry which tenants are actually relying on their override.

**`success_condition` cannot be overridden.** A tenant may differ in how it labels a
button or how many steps a flow takes. It may not redefine what success *means*, because
that is the contract the calling agent relies on, and a capability that means different
things at different institutions is not one capability.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from cua.schema.capability import Capability, Step, StepValue
from cua.schema.locator import Locator, LocatorStrategy


class TargetOverride(BaseModel):
    """A per-tenant adjustment to how one control is found or filled."""

    prepend_strategies: list[LocatorStrategy] = Field(
        default_factory=list,
        description="Tried before the recorded ladder. Additive, so the base strategies "
        "remain as fallbacks.",
    )
    skip: bool = Field(default=False, description="This tenant's flow does not include this step.")
    value_literal: str | None = Field(
        default=None, description="Override the literal typed or selected at this step."
    )
    describe: str = Field(default="", description="Why this tenant needs the override.")


class CapabilityOverride(BaseModel):
    version_range: str = Field(
        default="*",
        description="Which capability versions this overlay applies to. An overlay "
        "written against a flow that has since changed shape should not silently apply "
        "to the new one.",
    )
    steps: dict[str, TargetOverride] = Field(default_factory=dict)
    outputs: dict[str, TargetOverride] = Field(
        default_factory=dict,
        description="Output extraction differs when a tenant renders the same data in a "
        "different structure - a table on one, a definition list on another.",
    )
    insert_before: dict[str, list[Step]] = Field(
        default_factory=dict,
        description="Extra steps this tenant requires, keyed by the step id they precede. "
        "Some tenants add a mandatory field rather than merely renaming one, and a "
        "rename-only overlay model could not express that.",
    )


class TenantProfile(BaseModel):
    tenant_id: str
    display_name: str
    base_url: str = Field(description="Substituted for {{base_url}} in the artifact.")
    product_version: str = Field(
        default="",
        description="Which build of the vendor product this institution runs. Two "
        "tenants on the same product at different versions are the common case.",
    )
    vars: dict[str, str] = Field(default_factory=dict)
    capability_overrides: dict[str, CapabilityOverride] = Field(default_factory=dict)

    def override_for(self, capability_id: str) -> CapabilityOverride | None:
        return self.capability_overrides.get(capability_id)


def resolve_for_tenant(capability: Capability, profile: TenantProfile) -> Capability:
    """Compose a capability with one tenant's overlay. Pure; the artifact is untouched.

    Returns the capability unchanged when the tenant has no overrides, which is the
    common case and the whole point: most institutions running the same product need no
    specialisation at all.
    """
    override = profile.override_for(capability.id)
    if override is None:
        return capability

    steps: list[Step] = []
    for step in capability.steps:
        for extra in override.insert_before.get(step.id, []):
            steps.append(extra)

        adjustment = override.steps.get(step.id)
        if adjustment is None:
            steps.append(step)
            continue
        if adjustment.skip:
            continue
        steps.append(_apply_to_step(step, adjustment))

    outputs = [
        (
            o.model_copy(update={"locator": _prepend(o.locator, override.outputs[o.name])})
            if o.name in override.outputs and o.locator is not None
            else o
        )
        for o in capability.outputs
    ]

    # `success_condition` is deliberately absent from this update: a tenant may not
    # redefine what success means.
    return capability.model_copy(update={"steps": steps, "outputs": outputs})


def _apply_to_step(step: Step, adjustment: TargetOverride) -> Step:
    update: dict[str, object] = {}
    if adjustment.prepend_strategies and step.target is not None:
        update["target"] = _prepend(step.target, adjustment)
    if adjustment.value_literal is not None:
        update["value"] = StepValue(literal=adjustment.value_literal)
    return step.model_copy(update=update) if update else step


def _prepend(locator: Locator | None, adjustment: TargetOverride) -> Locator | None:
    if locator is None:
        return None
    return locator.model_copy(
        update={"strategies": [*adjustment.prepend_strategies, *locator.strategies]}
    )
