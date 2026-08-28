"""Matching a locator strategy against an observation.

Deliberately **pure**: strategies are matched against `ElementNode`s, not against a live
browser. Three things follow from that, and they are the reason for the design.

1.  The entire locator ladder is unit-testable with no browser and no network. The most
    load-bearing logic in the system runs in milliseconds in CI.
2.  Generation and resolution share one matcher, so a strategy recorded because it
    uniquely matched cannot later be resolved by different rules and pick something else.
3.  It works for things a selector engine cannot express. "The Action cell in the row
    where Member ID = 100042", or "the button named Search *inside the Member Search
    section*", are relational queries over perceived structure, not CSS.

The cost, stated honestly: we match on our own accessible-name computation (see
snapshot.py) rather than Playwright's authoritative accname implementation. For enterprise
forms the subset we implement is sufficient, and internal consistency matters more here
than spec fidelity - a mismatch between how we record and how we resolve would be far
worse than both being slightly non-standard in the same way.
"""

from __future__ import annotations

import re
import unicodedata

from cua.schema.locator import (
    Locator,
    LocatorStrategy,
    NameMatch,
    Resolution,
    ResolveOutcome,
    Tier,
)
from cua.surface.base import ElementNode, Observation

#: Distance in pixels within which a bbox strategy still counts as the same control.
#: Generous, because tier 7 is a last resort on surfaces where nothing better exists,
#: and a control that moved 30px is almost certainly still that control.
BBOX_TOLERANCE_PX = 30.0

_PUNCT = re.compile(r"[^\w\s]+")
_SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Fold the differences that are cosmetic rather than semantic.

    "Find Member", "find member", and "Find  Member:" all normalise together. This is
    what lets one recorded locator survive the most common form of per-tenant drift -
    the same control relabelled - without weakening the match to a substring, which
    would happily confuse "Search" with "Advanced Search".
    """
    folded = unicodedata.normalize("NFKD", text).casefold()
    return _SPACE.sub(" ", _PUNCT.sub(" ", folded)).strip()


def _name_matches(candidate: str, strategy: LocatorStrategy) -> bool:
    expected = strategy.name or ""
    if strategy.name_match is NameMatch.EXACT:
        return candidate == expected
    if strategy.name_match is NameMatch.NORMALISED:
        return normalise(candidate) == normalise(expected)
    try:
        return re.search(expected, candidate) is not None
    except re.error:
        return False


def _in_frame(node: ElementNode, frame_path: list[str]) -> bool:
    return node.frame_path == frame_path


def _in_scope(node: ElementNode, strategy: LocatorStrategy) -> bool:
    if strategy.scope_section is None:
        return True
    return node.section == strategy.scope_section


def match_strategy(
    strategy: LocatorStrategy,
    observation: Observation,
    frame_path: list[str],
    *,
    any_frame: bool = False,
) -> list[ElementNode]:
    """Every element in `observation` this strategy selects. Zero, one, or many."""
    pool = (
        list(observation.elements)
        if any_frame
        else [n for n in observation.elements if _in_frame(n, frame_path)]
    )

    if strategy.tier in (Tier.ROLE_NAME_EXACT, Tier.ROLE_NAME_NORMALISED):
        return [
            n
            for n in pool
            if n.role == strategy.role
            and _in_scope(n, strategy)
            and _name_matches(n.name, strategy)
        ]

    if strategy.tier is Tier.LABEL:
        # A form control whose accessible name came from its <label>.
        return [
            n
            for n in pool
            if n.role in _FORM_ROLES and normalise(n.name) == normalise(strategy.label or "")
        ]

    if strategy.tier is Tier.ROW_CELL:
        wanted = strategy.row_key or {}
        return [
            n
            for n in pool
            if n.row_context is not None
            and n.row_context.column_header == (strategy.column_header or "")
            and all(n.row_context.row_key.get(k) == v for k, v in wanted.items())
        ]

    if strategy.tier is Tier.TEXT_ANCHOR:
        return [
            n
            for n in pool
            if n.anchor_text is not None
            and normalise(n.anchor_text) == normalise(strategy.anchor_text or "")
            and (strategy.role is None or n.role == strategy.role)
        ]

    if strategy.tier is Tier.ROLE_ORDINAL:
        same = [n for n in pool if n.role == strategy.role and _in_scope(n, strategy)]
        idx = strategy.ordinal or 0
        return [same[idx]] if 0 <= idx < len(same) else []

    if strategy.tier is Tier.BBOX and strategy.bbox is not None:
        box = strategy.bbox
        return [
            n
            for n in pool
            if abs(n.bbox.x - box.x) <= BBOX_TOLERANCE_PX
            and abs(n.bbox.y - box.y) <= BBOX_TOLERANCE_PX
        ]

    return []


_FORM_ROLES = frozenset({"textbox", "searchbox", "combobox", "checkbox", "radio", "spinbutton"})


def resolve(locator: Locator, observation: Observation) -> Resolution:
    """Walk the ladder. First strategy that uniquely resolves wins.

    Three rules, and they are what make this rigorous rather than a pile of fallbacks:

    * **Uniqueness is required.** A strategy matching two elements is skipped, not
      guessed at. If every strategy is either empty or ambiguous, the result is
      AMBIGUOUS rather than a coin flip - picking `.first` is how replay silently does
      the wrong thing to the wrong member.
    * **Strategies cross-validate.** Lower-ranked strategies still run, and if one
      uniquely resolves to a *different* element it is recorded as disagreeing. That
      is a drift signal worth surfacing, not something to quietly discard.
    * **The winning tier is reported.** A step recorded at tier 1 that now resolves at
      tier 4 means a label changed. Cheap to record, and it is the earliest warning
      available that a tenant's UI has moved.
    """
    tiers_tried: list[Tier] = []
    winner: ElementNode | None = None
    winning_tier: Tier | None = None
    disagreeing: list[Tier] = []
    saw_ambiguous = False

    for strategy in sorted(locator.strategies, key=lambda s: s.tier):
        tiers_tried.append(strategy.tier)
        matches = match_strategy(
            strategy, observation, locator.frame_path, any_frame=locator.any_frame
        )

        if len(matches) != 1:
            saw_ambiguous = saw_ambiguous or len(matches) > 1
            continue

        found = matches[0]
        if winner is None:
            winner, winning_tier = found, strategy.tier
        elif found.ref != winner.ref:
            disagreeing.append(strategy.tier)

    if winner is None:
        return Resolution(
            outcome=ResolveOutcome.AMBIGUOUS if saw_ambiguous else ResolveOutcome.NOT_FOUND,
            tiers_tried=tiers_tried,
            detail=(
                "every strategy matched more than one element; refusing to guess"
                if saw_ambiguous
                else "no strategy matched any element"
            ),
        )

    return Resolution(
        outcome=ResolveOutcome.RESOLVED,
        ref=winner.ref,
        winning_tier=winning_tier,
        tiers_tried=tiers_tried,
        disagreeing_tiers=disagreeing,
        detail=winner.describe(),
    )
