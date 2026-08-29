"""The locator ladder, tested without a browser.

Every test here encodes a claim that has to survive an interview. They run in
milliseconds because matching is a pure function over `ElementNode`s - which is most of
the reason the design is shaped that way.
"""

from __future__ import annotations

import pytest

from cua.locator.generate import bind, generate, parameterise
from cua.locator.match import normalise, resolve
from cua.schema.common import BBox, RowContext
from cua.schema.locator import Locator, LocatorStrategy, NameMatch, ResolveOutcome, Tier
from cua.surface.base import ElementNode, Observation

pytestmark = pytest.mark.unit


def node(
    ref: str,
    role: str,
    name: str,
    *,
    section: str | None = None,
    frame: list[str] | None = None,
    row: dict[str, str] | None = None,
    column: str = "",
    anchor: str | None = None,
    x: float = 0,
    y: float = 0,
) -> ElementNode:
    return ElementNode(
        ref=ref,
        role=role,
        name=name,
        section=section,
        frame_path=frame if frame is not None else ["contentFrame"],
        row_context=RowContext(row_key=row, column_header=column) if row else None,
        anchor_text=anchor,
        bbox=BBox(x=x, y=y, w=60, h=20),
    )


def obs(*nodes: ElementNode) -> Observation:
    return Observation(observation_id="o1", url="http://x/", title="t", elements=list(nodes))


# ------------------------------------------------------------------ search results grid


def grid() -> Observation:
    """Two rows, so "the View link" is genuinely ambiguous without row context."""
    rows = [
        {"Member ID": "100042", "Name": "J. RIVERA", "Status": "ACTIVE"},
        {"Member ID": "100043", "Name": "M. CHEN", "Status": "DORMANT"},
    ]
    nodes = [node("e0", "heading", "Search Results")]
    for i, row in enumerate(rows):
        for col, val in row.items():
            nodes.append(
                node(f"e{i}_{col}", "cell", val, section="Search Results", row=row, column=col)
            )
        nodes.append(
            node(
                f"e{i}_view",
                "link",
                "View",
                section="Search Results",
                row=row,
                column="Action",
                y=100 + i * 20,
            )
        )
    return obs(*nodes)


def test_row_context_disambiguates_an_otherwise_identical_control() -> None:
    """Tier 4's reason for existing.

    Both rows have a link named exactly "View" in the same section. Role+name is
    ambiguous and is therefore *not recorded*; the row strategy is.
    """
    observation = grid()
    target = next(n for n in observation.elements if n.ref == "e1_view")

    locator = generate(target, observation)
    tiers = [s.tier for s in locator.strategies]

    assert Tier.ROLE_NAME_EXACT not in tiers, "an ambiguous strategy was recorded"
    assert Tier.ROW_CELL in tiers

    resolution = resolve(locator, observation)
    assert resolution.outcome is ResolveOutcome.RESOLVED
    assert resolution.ref == "e1_view"
    assert resolution.winning_tier is Tier.ROW_CELL


def test_ambiguity_is_refused_rather_than_guessed() -> None:
    """Picking `.first` is how replay silently acts on the wrong member."""
    observation = grid()
    ambiguous = Locator(
        describe="a View link",
        frame_path=["contentFrame"],
        strategies=[
            LocatorStrategy(tier=Tier.ROLE_NAME_EXACT, role="link", name="View"),
        ],
    )

    resolution = resolve(ambiguous, observation)
    assert resolution.outcome is ResolveOutcome.AMBIGUOUS
    assert resolution.ref is None


def test_unmatched_locator_is_not_found_not_ambiguous() -> None:
    resolution = resolve(
        Locator(
            describe="a missing button",
            frame_path=["contentFrame"],
            strategies=[LocatorStrategy(tier=Tier.ROLE_NAME_EXACT, role="button", name="Nope")],
        ),
        grid(),
    )
    assert resolution.outcome is ResolveOutcome.NOT_FOUND


# ------------------------------------------------------------------ tier ordering & drift


def search_screen(button_label: str = "Search", field_label: str = "Member ID") -> Observation:
    return obs(
        node("e0", "heading", "Member Search"),
        node("e1", "textbox", field_label, section="Member Search"),
        node("e2", "button", button_label, section="Member Search", x=200),
        node("e3", "button", "Clear", section="Member Search", x=280),
    )


def test_exact_name_wins_when_nothing_has_changed() -> None:
    observation = search_screen()
    target = next(n for n in observation.elements if n.ref == "e2")

    resolution = resolve(generate(target, observation), observation)
    assert resolution.winning_tier is Tier.ROLE_NAME_EXACT
    assert not resolution.degraded


def test_a_relabelled_control_still_resolves_and_reports_degradation() -> None:
    """The cross-tenant case, and the drift signal, in one test.

    A capability recorded against a tenant whose button says "Search" is replayed
    against one that says "Find Member". Tier 1 fails. A lower tier carries the step,
    and `degraded` records that a better strategy stopped working - which is precisely
    the early warning you want per tenant, before it becomes an outage.
    """
    recorded = generate(
        next(n for n in search_screen().elements if n.ref == "e2"),
        search_screen(),
    )
    other_tenant = search_screen(button_label="Find Member", field_label="Member Number")

    resolution = resolve(recorded, other_tenant)

    assert resolution.outcome is ResolveOutcome.RESOLVED
    assert resolution.ref == "e2"
    assert resolution.degraded, "tier degradation should be reported"
    assert Tier.ROLE_NAME_EXACT in resolution.tiers_tried


def test_normalised_names_absorb_cosmetic_drift() -> None:
    assert normalise("Find  Member:") == normalise("find member")
    assert normalise("Search") != normalise("Advanced Search")

    observation = search_screen(button_label="  SEARCH: ")
    locator = Locator(
        describe="search",
        frame_path=["contentFrame"],
        strategies=[
            LocatorStrategy(
                tier=Tier.ROLE_NAME_NORMALISED,
                role="button",
                name="Search",
                name_match=NameMatch.NORMALISED,
            )
        ],
    )
    assert resolve(locator, observation).ref == "e2"


def test_section_scoping_prevents_a_name_collision_across_the_page() -> None:
    observation = obs(
        node("e0", "heading", "Member Search"),
        node("e1", "button", "Submit", section="Member Search"),
        node("e2", "heading", "Address Change"),
        node("e3", "button", "Submit", section="Address Change"),
    )
    target = next(n for n in observation.elements if n.ref == "e3")

    resolution = resolve(generate(target, observation), observation)
    assert resolution.ref == "e3"
    assert resolution.winning_tier is Tier.ROLE_NAME_EXACT


def test_frame_path_is_part_of_identity() -> None:
    """Two frames can hold identically named controls; a locator must not cross frames."""
    observation = obs(
        node("e0", "button", "Search", frame=["navFrame"]),
        node("e1", "button", "Search", frame=["contentFrame"]),
    )
    target = next(n for n in observation.elements if n.ref == "e1")

    resolution = resolve(generate(target, observation), observation)
    assert resolution.ref == "e1"


# ------------------------------------------------------------------ header-less tables


def test_text_anchor_reaches_values_on_tables_with_no_headers() -> None:
    """The Meridian detail screen has no <th>, so tier 4 is unavailable.

    Without a text anchor the balance would not be addressable at all - which is why
    the ladder needs a rung between "grid semantics" and "coordinates".
    """
    observation = obs(
        node("e0", "heading", "Member Detail"),
        node("e1", "cell", "Member ID", section="Member Detail"),
        node("e2", "cell", "100042", section="Member Detail", anchor="Member ID"),
        node("e3", "cell", "Status", section="Member Detail"),
        node("e4", "cell", "ACTIVE", section="Member Detail", anchor="Status"),
    )
    target = next(n for n in observation.elements if n.ref == "e4")

    locator = generate(target, observation)
    assert Tier.TEXT_ANCHOR in [s.tier for s in locator.strategies]
    assert resolve(locator, observation).ref == "e4"


# ------------------------------------------------------------------ parameterisation


def test_recorded_row_values_become_parameters() -> None:
    """Recorded against member 100042, the tier-4 strategy names that member.

    Left alone, the capability would only ever work for one member. Parameterising the
    row key is what makes it a *capability* rather than a recording.
    """
    observation = grid()
    target = next(n for n in observation.elements if n.ref == "e0_view")

    recorded = parameterise(generate(target, observation), {"memberId": "100042"})
    row_strategy = next(s for s in recorded.strategies if s.tier is Tier.ROW_CELL)
    assert row_strategy.row_key is not None
    assert row_strategy.row_key["Member ID"] == "{{memberId}}"
    assert "{{memberId}}" in recorded.describe

    # Bound to a different member, it targets that member's row - not the recorded one.
    bound = bind(recorded, {"memberId": "100043"})
    assert resolve(bound, observation).ref == "e1_view"

    bound_original = bind(recorded, {"memberId": "100042"})
    assert resolve(bound_original, observation).ref == "e0_view"


def test_an_unparameterised_locator_is_unchanged_by_binding() -> None:
    observation = search_screen()
    locator = generate(next(n for n in observation.elements if n.ref == "e2"), observation)
    assert bind(locator, {"memberId": "100042"}) == locator


# ------------------------------------------------------------------ invariants


def test_only_uniquely_resolving_strategies_are_recorded() -> None:
    """Recording an ambiguous strategy would be recording a landmine: it looks like a
    fallback and behaves like a coin flip."""
    observation = grid()
    for target in observation.elements:
        locator = generate(target, observation)
        for strategy in locator.strategies:
            from cua.locator.match import match_strategy

            matches = match_strategy(strategy, observation, target.frame_path)
            assert len(matches) == 1 and matches[0].ref == target.ref, (
                f"{target.ref}: tier {strategy.tier} matched {len(matches)} elements"
            )


def test_every_element_gets_at_least_a_coordinate_strategy() -> None:
    """Tier 7 exists so the abstraction is honest about Citrix-style surfaces where
    nothing semantic is available. It must always be present."""
    observation = obs(node("e0", "cell", "", section=None))
    locator = generate(observation.elements[0], observation)
    assert [s.tier for s in locator.strategies][-1] is Tier.BBOX


def test_strategies_are_stored_best_first() -> None:
    observation = grid()
    target = next(n for n in observation.elements if n.ref == "e0_view")
    tiers = [s.tier for s in generate(target, observation).strategies]
    assert tiers == sorted(tiers)


def test_cross_validation_flags_strategies_that_disagree() -> None:
    """Two strategies each resolving uniquely, to different elements, is a drift signal
    rather than something to silently discard."""
    observation = obs(
        node("e0", "button", "Search", section="Member Search", x=10),
        node("e1", "button", "Go", section="Member Search", x=90),
    )
    conflicting = Locator(
        describe="conflicting",
        frame_path=["contentFrame"],
        strategies=[
            LocatorStrategy(tier=Tier.ROLE_NAME_EXACT, role="button", name="Search"),
            LocatorStrategy(tier=Tier.ROLE_ORDINAL, role="button", ordinal=1),
        ],
    )

    resolution = resolve(conflicting, observation)
    assert resolution.ref == "e0"
    assert Tier.ROLE_ORDINAL in resolution.disagreeing_tiers


def test_parameterised_locators_drop_strategies_that_ignore_the_parameter() -> None:
    """The most dangerous failure this design can have.

    Ordinal and bbox cannot express "{{memberId}}" - they resolve to whatever occupied
    that position when the flow was recorded. Kept as fallbacks on a parameterised
    locator, they turn "row not found" into "confidently operated on the wrong member".
    They are removed, not demoted.
    """
    observation = grid()
    target = next(n for n in observation.elements if n.ref == "e0_view")

    raw = generate(target, observation)
    assert Tier.ROLE_ORDINAL in [s.tier for s in raw.strategies]
    # bbox is already absent here: the two rows sit 20px apart, inside the tolerance, so
    # it was rejected as ambiguous at generation time. Uniqueness screening catches some
    # of these before parameterisation ever runs - but not all, hence the rule below.
    assert Tier.BBOX not in [s.tier for s in raw.strategies]

    recorded = parameterise(raw, {"memberId": "100042"})
    tiers = [s.tier for s in recorded.strategies]
    assert Tier.ROLE_ORDINAL not in tiers
    assert Tier.BBOX not in tiers
    assert tiers == [Tier.ROW_CELL]

    # And the consequence: a member that is not on screen fails loudly.
    missing = bind(recorded, {"memberId": "999999"})
    assert resolve(missing, observation).outcome is ResolveOutcome.NOT_FOUND


def test_parameterised_row_keys_are_minimal() -> None:
    """A row key holding the whole row pins the siblings to the recorded member, so the
    strategy matches nobody else. Only the identifying column survives."""
    observation = grid()
    target = next(n for n in observation.elements if n.ref == "e0_view")

    recorded = parameterise(generate(target, observation), {"memberId": "100042"})
    row = next(s for s in recorded.strategies if s.tier is Tier.ROW_CELL)

    assert row.row_key == {"Member ID": "{{memberId}}"}
    assert "Name" not in (row.row_key or {}), "correlated columns must not be pinned"


def test_assertion_locators_are_bound_like_step_locators() -> None:
    """A checkpoint on "the cell showing {{memberId}}" is a reasonable thing for a
    discovery run to record, so assertions must bind parameters exactly as actions do.

    Omitting that was a real bug, and one only a live model surfaced: every action
    resolved correctly and then the success condition failed, because it was hunting for
    a cell literally named "{{memberId}}". The scripted stand-in never recorded a
    parameterised checkpoint, so the gap stayed invisible.
    """
    from cua.replay.assertions import evaluate
    from cua.schema.capability import Assertion, AssertionKind

    observation = obs(
        node("e0", "heading", "Member Detail"),
        node("e1", "cell", "100042", section="Member Detail"),
    )
    parameterised = Locator(
        describe="the member id cell",
        frame_path=["contentFrame"],
        strategies=[
            LocatorStrategy(
                tier=Tier.ROLE_NAME_EXACT,
                role="cell",
                name="{{memberId}}",
                scope_section="Member Detail",
            )
        ],
    )
    assertion = Assertion(
        kind=AssertionKind.ELEMENT_PRESENT, locator=parameterised, describe="on detail"
    )

    assert not evaluate(assertion, observation).passed, "unbound should not match"
    assert evaluate(assertion, observation, None, {"memberId": "100042"}).passed
    assert not evaluate(assertion, observation, None, {"memberId": "999999"}).passed
