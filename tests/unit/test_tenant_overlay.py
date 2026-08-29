"""Cross-tenant reuse.

The brief's question: *"How would you represent an artifact so it can be reused (or
safely specialized/overridden) across tenants running the same app, rather than
re-recorded per tenant?"*

The answer these tests pin down is composition: an artifact describes the vendor product,
a tenant profile is a thin diff, and they are merged at replay time without the artifact
on disk ever changing. Two constraints matter as much as the mechanism - overlays add
strategies rather than replacing them, and no overlay may redefine what success means.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cua.schema.capability import (
    ActionKind,
    Assertion,
    AssertionKind,
    Capability,
    InputParam,
    OutputField,
    ProductRef,
    Step,
    StepValue,
    TargetRef,
)
from cua.schema.locator import Locator, LocatorStrategy, NameMatch, Tier
from cua.schema.tenant import (
    CapabilityOverride,
    TargetOverride,
    TenantProfile,
    resolve_for_tenant,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


def locator(name: str) -> Locator:
    return Locator(
        describe=f"the {name} control",
        frame_path=["contentFrame"],
        strategies=[
            LocatorStrategy(tier=Tier.ROLE_NAME_EXACT, role="button", name=name),
            LocatorStrategy(
                tier=Tier.ROLE_NAME_NORMALISED,
                role="button",
                name=name,
                name_match=NameMatch.NORMALISED,
            ),
        ],
    )


def capability() -> Capability:
    return Capability(
        id="member.savings-balance",
        version="1.0.0",
        display_name="Read savings balance",
        description="d",
        target=TargetRef(
            product=ProductRef(vendor="corelink", app="servicing-console"),
            entry_url_pattern="{{base_url}}/",
        ),
        inputs=[InputParam(name="memberId", example="100042")],
        outputs=[OutputField(name="savingsBalance", locator=locator("Balance"))],
        steps=[
            Step(
                id="s1",
                intent="type the member id",
                action=ActionKind.FILL,
                target=locator("Member ID"),
                value=StepValue(param="memberId"),
            ),
            Step(
                id="s2",
                intent="submit the search",
                action=ActionKind.CLICK,
                target=locator("Search"),
            ),
        ],
        success_condition=Assertion(
            kind=AssertionKind.OUTPUT_PRESENT, output="savingsBalance", describe="readable"
        ),
    )


def profile(**overrides: object) -> TenantProfile:
    return TenantProfile(
        tenant_id="lakeside",
        display_name="Lakeside Federal CU",
        base_url="http://x/tenants/lakeside",
        capability_overrides={"member.savings-balance": CapabilityOverride(**overrides)},  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------ the default case


def test_a_tenant_with_no_overrides_gets_the_artifact_unchanged() -> None:
    """The common case, and the whole point: most institutions running the same product
    need no specialisation at all."""
    base = capability()
    plain = TenantProfile(tenant_id="meridian", display_name="Meridian", base_url="http://x")
    assert resolve_for_tenant(base, plain) is base


def test_composition_never_mutates_the_artifact_on_disk() -> None:
    """`git diff` on a capability shows a change to the product automation; `git diff` on
    a tenant profile shows one institution's specialisation. Merging them would make
    both unreviewable."""
    base = capability()
    before = base.model_dump_json()

    resolve_for_tenant(
        base,
        profile(
            steps={
                "s2": TargetOverride(
                    prepend_strategies=[
                        LocatorStrategy(
                            tier=Tier.ROLE_NAME_EXACT, role="button", name="Find Member"
                        )
                    ]
                )
            }
        ),
    )
    assert base.model_dump_json() == before


# ------------------------------------------------------------------ additive overrides


def test_overrides_are_prepended_so_the_recorded_ladder_survives() -> None:
    """A tenant adds a way to find a control; it does not remove the recorded ones.

    So an institution that quietly reverts to the standard labelling keeps working - and
    because the winning tier is reported, you can see which tenants actually rely on
    their override.
    """
    resolved = resolve_for_tenant(
        capability(),
        profile(
            steps={
                "s2": TargetOverride(
                    describe="Lakeside calls it Find Member",
                    prepend_strategies=[
                        LocatorStrategy(
                            tier=Tier.ROLE_NAME_EXACT, role="button", name="Find Member"
                        )
                    ],
                )
            }
        ),
    )
    names = [s.name for s in resolved.steps[1].target.strategies]  # type: ignore[union-attr]
    assert names[0] == "Find Member"
    assert "Search" in names, "the recorded strategy was dropped rather than demoted"


def test_a_tenant_can_require_an_extra_step() -> None:
    """Some institutions add a mandatory field rather than merely renaming one. An
    overlay model that only expressed renames could not describe Lakeside at all."""
    branch = Step(
        id="lakeside-branch",
        intent="Lakeside requires a branch before searching",
        action=ActionKind.SELECT,
        target=locator("Branch"),
        value=StepValue(literal="MAIN"),
    )
    resolved = resolve_for_tenant(capability(), profile(insert_before={"s2": [branch]}))

    assert [s.id for s in resolved.steps] == ["s1", "lakeside-branch", "s2"]


def test_a_tenant_can_skip_a_step_it_does_not_have() -> None:
    resolved = resolve_for_tenant(capability(), profile(steps={"s1": TargetOverride(skip=True)}))
    assert [s.id for s in resolved.steps] == ["s2"]


def test_output_extraction_can_be_overridden_independently() -> None:
    """The same data rendered differently - a table at one institution, a definition
    list at another - is a change to where a value *is*, not to what it means."""
    resolved = resolve_for_tenant(
        capability(),
        profile(
            outputs={
                "savingsBalance": TargetOverride(
                    prepend_strategies=[
                        LocatorStrategy(
                            tier=Tier.TEXT_ANCHOR, role="definition", anchor_text="Savings"
                        )
                    ]
                )
            }
        ),
    )
    strategies = resolved.outputs[0].locator.strategies  # type: ignore[union-attr]
    assert strategies[0].tier is Tier.TEXT_ANCHOR
    assert strategies[0].role == "definition"


# ------------------------------------------------------------------ the hard constraint


def test_a_tenant_cannot_redefine_what_success_means() -> None:
    """A tenant may differ in how it labels a button or how many steps a flow takes. It
    may not change the contract the calling agent relies on - a capability that means
    different things at different institutions is not one capability."""
    base = capability()
    resolved = resolve_for_tenant(
        base,
        profile(
            steps={
                "s2": TargetOverride(
                    prepend_strategies=[
                        LocatorStrategy(
                            tier=Tier.ROLE_NAME_EXACT, role="button", name="Find Member"
                        )
                    ]
                )
            }
        ),
    )
    assert resolved.success_condition == base.success_condition
    assert "success_condition" not in CapabilityOverride.model_fields


def test_the_success_condition_follows_an_output_override_without_being_overridden() -> None:
    """Why success conditions name outputs rather than embedding locators.

    Lakeside renders the balance somewhere else. The *meaning* of success is unchanged -
    "the balance is readable" - and it resolves through whichever locator is in force,
    so the tenant never has to touch the contract.
    """
    resolved = resolve_for_tenant(
        capability(),
        profile(
            outputs={
                "savingsBalance": TargetOverride(
                    prepend_strategies=[
                        LocatorStrategy(
                            tier=Tier.TEXT_ANCHOR, role="definition", anchor_text="Savings"
                        )
                    ]
                )
            }
        ),
    )
    assert resolved.success_condition is not None
    assert resolved.success_condition.kind is AssertionKind.OUTPUT_PRESENT
    assert resolved.success_condition.locator is None, "no locator is embedded to go stale"
    assert resolved.outputs[0].locator.strategies[0].role == "definition"  # type: ignore[union-attr]


# ------------------------------------------------------------------ the shipped profiles


def test_the_shipped_lakeside_overlay_is_a_thin_diff() -> None:
    """Two renamed controls, one extra step, one relocated output - against a seven-step
    flow. If an overlay had to describe the whole flow it would not be reuse."""
    lakeside = TenantProfile.model_validate_json((REPO / "tenants" / "lakeside.json").read_text())
    override = lakeside.override_for("member.savings-balance")
    assert override is not None
    assert len(override.steps) == 2
    assert len(override.insert_before) == 1
    assert len(override.outputs) == 1
    for adjustment in override.steps.values():
        assert adjustment.describe, "an override should say why it exists"


def test_the_shipped_profiles_are_valid_and_distinct() -> None:
    profiles = [
        TenantProfile.model_validate_json(p.read_text())
        for p in sorted((REPO / "tenants").glob("*.json"))
    ]
    assert len(profiles) >= 2
    assert len({p.tenant_id for p in profiles}) == len(profiles)
    assert len({p.base_url for p in profiles}) == len(profiles)


def test_the_capability_itself_names_a_product_not_a_tenant() -> None:
    """Roughly 2,000 app instances but only ~20 products - so the unit of authoring has
    to be the product."""
    artifact = json.loads((REPO / "capabilities" / "member.savings-balance@1.0.0.json").read_text())
    assert artifact["target"]["product"]["vendor"]
    assert artifact["target"]["product"]["app"]
    assert "{{base_url}}" in artifact["target"]["entry_url_pattern"]
    assert "meridian" not in json.dumps(artifact["steps"]), (
        "a tenant name leaked into the product-level flow"
    )
