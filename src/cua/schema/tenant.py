"""Tenant profiles and overlays.

An artifact targets a *vendor product*; a tenant is a deployment of that product,
configured and branded differently. Roughly 100 institutions running ~20 apps each is
2,000 instances but only ~20 products, so the unit of authoring has to be the product
and the tenant has to be a thin, reviewable diff on top.

Composition happens at replay time and never mutates the artifact on disk. That matters
for review: `git diff` on a capability shows a change to the *product* automation, and
`git diff` on a tenant profile shows one institution's specialisation. Merging them into
one file would make both unreviewable.

The one thing a tenant may **not** override is `success_condition`. A tenant can differ
in how it labels a button or how many steps a flow takes; it cannot redefine what
success means, because that is the contract the calling agent relies on.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from cua.schema.locator import LocatorStrategy


class StepOverride(BaseModel):
    """A per-tenant adjustment to one recorded step."""

    #: Extra strategies tried *before* the recorded ones. Additive rather than
    #: replacing, so the base ladder stays as a fallback and a tenant that quietly
    #: reverts to the standard labelling keeps working.
    prepend_strategies: list[LocatorStrategy] = Field(default_factory=list)
    #: This tenant does not have this step at all.
    skip: bool = False
    #: Override the value typed at this step.
    value_literal: str | None = None


class CapabilityOverride(BaseModel):
    version_range: str = "*"
    steps: dict[str, StepOverride] = Field(default_factory=dict)
    #: Extra steps this tenant requires, keyed by the step id to insert before.
    insert_before: dict[str, list[str]] = Field(
        default_factory=dict,
        description="step_id -> ids of extra steps, defined in `extra_steps`.",
    )


class TenantProfile(BaseModel):
    tenant_id: str
    display_name: str
    base_url: str = Field(description="Substituted for {{base_url}} in the artifact.")
    #: Free-form values a capability can reference, e.g. a default branch code.
    vars: dict[str, str] = Field(default_factory=dict)
    capability_overrides: dict[str, CapabilityOverride] = Field(default_factory=dict)

    def override_for(self, capability_id: str) -> CapabilityOverride | None:
        return self.capability_overrides.get(capability_id)
