"""Turning a perceived element into a durable, ranked identity.

Called at the moment the model (or a human) acts on something. The ephemeral ref `e14`
is thrown away; what gets recorded into the artifact is every way of describing that
element that **uniquely** identifies it right now.

Generating several strategies rather than one is the whole robustness story. A single
locator is a bet on one property of the UI staying put. A ranked ladder is a bet that at
least one of six independent properties will - and it tells you, via the winning tier,
which of those bets you lost.

A strategy is only recorded if it resolves to exactly one element in the observation it
was generated from. Recording an ambiguous strategy would be recording a landmine: it
looks like a fallback and behaves like a coin flip.
"""

from __future__ import annotations

from cua.locator.match import match_strategy, normalise
from cua.schema.common import BBox
from cua.schema.locator import Locator, LocatorStrategy, NameMatch, Tier
from cua.surface.base import ElementNode, Observation

_FORM_ROLES = frozenset({"textbox", "searchbox", "combobox", "checkbox", "radio", "spinbutton"})

#: Roles whose accessible name *is* their content, rather than a label describing it.
#: For these, a name-based strategy on an element you intend to *read* is circular.
CONTENT_ROLES = frozenset({"cell", "columnheader", "rowheader", "definition", "term"})


def describe_element(node: ElementNode, *, for_extraction: bool = False) -> str:
    """The prose a human reviewer reads when approving the capability.

    For an extraction target the current *value* is deliberately left out. It is the
    thing being read, it is frequently regulated data (a balance), and an artifact is a
    committed file - so "the Balance cell in the row where Account = Savings" is both
    safer and more accurate than quoting what it happened to say when recorded.
    """
    hide_value = for_extraction and node.role in CONTENT_ROLES
    column = node.row_context.column_header if node.row_context else ""
    parts = [f"the {column} {node.role}".strip() if hide_value else f"the {node.role}"]
    if node.name and not hide_value:
        parts.append(f"{node.name!r}")
    if node.row_context and node.row_context.row_key:
        own = node.row_context.column_header
        others = [(k, v) for k, v in node.row_context.row_key.items() if k != own]
        if others:
            key = ", ".join(f"{k} = {v}" for k, v in others[:2])
            parts.append(f"in the row where {key}")
    elif node.section:
        parts.append(f"in the {node.section!r} section")
    if node.frame_path:
        parts.append(f"(frame {'/'.join(node.frame_path)})")
    return " ".join(parts)


def generate(
    node: ElementNode,
    observation: Observation,
    *,
    describe: str | None = None,
    for_extraction: bool = False,
) -> Locator:
    """Build the ranked ladder for `node`, keeping only strategies that resolve uniquely.

    `for_extraction` marks a target whose *value* is the thing being read. For a table
    cell the accessible name and the value are the same string, so "the cell named
    $4,182.55" is a locator that already knows the answer - it works exactly once, and
    breaks the moment the balance changes, which for a balance is the normal case. Those
    strategies are dropped rather than demoted, for the same reason parameter-blind
    strategies are: a fallback that resolves confidently to the wrong thing is worse
    than having no fallback at all.
    """
    candidates = _candidates(node, observation)

    if for_extraction and node.role in CONTENT_ROLES:
        candidates = [
            s for s in candidates if s.tier not in (Tier.ROLE_NAME_EXACT, Tier.ROLE_NAME_NORMALISED)
        ]

    kept: list[LocatorStrategy] = []
    for strategy in candidates:
        matches = match_strategy(strategy, observation, node.frame_path)
        if len(matches) == 1 and matches[0].ref == node.ref:
            kept.append(strategy)

    kept.sort(key=lambda s: s.tier)
    return Locator(
        describe=describe or describe_element(node, for_extraction=for_extraction),
        frame_path=list(node.frame_path),
        strategies=kept,
    )


def _candidates(node: ElementNode, observation: Observation) -> list[LocatorStrategy]:
    out: list[LocatorStrategy] = []

    if node.name:
        # Tier 1 - the strongest identity: this role, this exact visible name, in this
        # part of the screen. Scoped by section so "Search" cannot drift onto a Search
        # control that lives somewhere else entirely.
        out.append(
            LocatorStrategy(
                tier=Tier.ROLE_NAME_EXACT,
                role=node.role,
                name=node.name,
                name_match=NameMatch.EXACT,
                scope_section=node.section,
                note="role + exact accessible name, scoped to its section",
            )
        )
        # Tier 2 - the same, case/whitespace/punctuation-folded and unscoped. Absorbs
        # cosmetic relabelling and section headings that differ between tenants.
        out.append(
            LocatorStrategy(
                tier=Tier.ROLE_NAME_NORMALISED,
                role=node.role,
                name=node.name,
                name_match=NameMatch.NORMALISED,
                note="role + normalised name; survives case and punctuation drift",
            )
        )

    if node.role in _FORM_ROLES and node.name:
        # Tier 3 - legacy forms rot in every way except their <label for>, which tends
        # to survive because the app depends on it for click-to-focus.
        out.append(
            LocatorStrategy(
                tier=Tier.LABEL,
                label=node.name,
                note="associated <label> text",
            )
        )

    if node.row_context and node.row_context.row_key:
        # Tier 4 - the only reliable way to target a control in a data grid, where the
        # accessible name ("View") is identical on every row. Row addressed by data,
        # column by header text; neither is positional.
        #
        # The element's *own* column is excluded from the row key. Including it makes
        # the locator circular for anything being read - "find the cell whose Balance is
        # $4,182.55, in order to read the Balance" - so it would break the moment the
        # value changed, which for a balance is the normal case rather than an edge one.
        own_column = node.row_context.column_header
        row_key = {k: v for k, v in node.row_context.row_key.items() if k != own_column}
        if row_key:
            out.append(
                LocatorStrategy(
                    tier=Tier.ROW_CELL,
                    row_key=row_key,
                    column_header=own_column,
                    note="data-grid row identified by other cells in the row, column by "
                    "header text; the target's own column is excluded so reading a "
                    "value does not depend on already knowing it",
                )
            )

    if node.anchor_text:
        # Tier 5 - header-less layout tables, i.e. every legacy detail screen:
        # <td>Member ID</td><td>100042</td>. Without this, values on those screens are
        # not addressable at all.
        out.append(
            LocatorStrategy(
                tier=Tier.TEXT_ANCHOR,
                role=node.role,
                anchor_text=node.anchor_text,
                note="the element immediately following a fixed label text",
            )
        )

    ordinal = _ordinal(node, observation)
    if ordinal is not None:
        # Tier 6 - positional, and therefore weak: it breaks the moment a control is
        # inserted. Recorded anyway because on unnamed controls it may be all there is,
        # and because a step that falls through to here is a loud drift signal.
        out.append(
            LocatorStrategy(
                tier=Tier.ROLE_ORDINAL,
                role=node.role,
                ordinal=ordinal,
                scope_section=node.section,
                note="positional fallback; weak, and a signal if it ever wins",
            )
        )

    # Tier 7 - coordinates. Present so the abstraction is not lying about surfaces where
    # nothing better exists (Citrix, canvas, remote desktop). Ranked last so it is never
    # reached while any semantic strategy still resolves.
    out.append(
        LocatorStrategy(
            tier=Tier.BBOX,
            bbox=BBox(x=node.bbox.x, y=node.bbox.y, w=node.bbox.w, h=node.bbox.h),
            note="coordinates; last resort for surfaces with no semantic layer",
        )
    )
    return out


def _ordinal(node: ElementNode, observation: Observation) -> int | None:
    same = [
        n
        for n in observation.elements
        if n.frame_path == node.frame_path and n.role == node.role and n.section == node.section
    ]
    try:
        return same.index(node)
    except ValueError:
        return None


def _is_parameterised(strategy: LocatorStrategy) -> bool:
    fields = [strategy.name, strategy.anchor_text, *(strategy.row_key or {}).values()]
    return any(f and "{{" in f for f in fields)


def parameterise(locator: Locator, values: dict[str, str]) -> Locator:
    """Replace concrete values with `{{param}}` placeholders.

    A locator recorded during discovery names the literal the model happened to use -
    "the View link in the row where Member ID = 100042". Replayed for a different member
    that row does not exist, so the artifact has to say "where Member ID = {{memberId}}".
    Without this the capability would work for exactly one member and would not be
    parameterised at all.

    Two safety rules here, both learned the hard way from
    `test_recorded_row_values_become_parameters`:

    **Minimal row keys.** A recorded row key holds the whole row - Member ID, Name,
    Status. Substituting only the id leaves the siblings pinned to the *recorded*
    member, so the strategy matches nothing for anyone else. When a row key contains a
    parameter, the other columns are correlated data that varies with it, so they are
    dropped and the parameterised column identifies the row on its own.

    **Parameter-blind strategies are removed, not demoted.** Ordinal and bbox cannot
    express a parameter: they would resolve to whatever sat in that position when the
    flow was recorded. For a parameterised target that is not a fallback, it is a
    confidently wrong answer - the exact "acts on the wrong member" failure this whole
    design exists to prevent. Once a locator depends on a runtime value, every strategy
    that ignores that value has to go, even if it means fewer rungs on the ladder.
    """
    lookup = {v: f"{{{{{name}}}}}" for name, v in values.items() if v}
    if not lookup:
        return locator

    swapped: list[LocatorStrategy] = []
    for strategy in locator.strategies:
        if strategy.row_key:
            substituted = {k: lookup.get(v, v) for k, v in strategy.row_key.items()}
            minimal = {k: v for k, v in substituted.items() if "{{" in v}
            strategy = strategy.model_copy(update={"row_key": minimal or substituted})
        if strategy.name and strategy.name in lookup:
            strategy = strategy.model_copy(update={"name": lookup[strategy.name]})
        if strategy.anchor_text and strategy.anchor_text in lookup:
            strategy = strategy.model_copy(update={"anchor_text": lookup[strategy.anchor_text]})
        swapped.append(strategy)

    if any(_is_parameterised(s) for s in swapped):
        swapped = [s for s in swapped if _is_parameterised(s)]

    describe = locator.describe
    for value, placeholder in lookup.items():
        describe = describe.replace(value, placeholder)
    return locator.model_copy(update={"strategies": swapped, "describe": describe})


def bind(locator: Locator, values: dict[str, str]) -> Locator:
    """The inverse: substitute `{{param}}` placeholders with this invocation's values.

    Applied at replay time, immediately before resolution. Kept separate from matching so
    the matcher stays a pure function over concrete data and never has to know that
    parameters exist.
    """
    if not values:
        return locator

    def sub(text: str) -> str:
        for name, value in values.items():
            text = text.replace(f"{{{{{name}}}}}", str(value))
        return text

    bound: list[LocatorStrategy] = []
    for strategy in locator.strategies:
        update: dict[str, object] = {}
        if strategy.row_key:
            update["row_key"] = {k: sub(v) for k, v in strategy.row_key.items()}
        if strategy.name:
            update["name"] = sub(strategy.name)
        if strategy.anchor_text:
            update["anchor_text"] = sub(strategy.anchor_text)
        bound.append(strategy.model_copy(update=update) if update else strategy)

    return locator.model_copy(update={"strategies": bound, "describe": sub(locator.describe)})


__all__ = [
    "CONTENT_ROLES",
    "bind",
    "describe_element",
    "generate",
    "normalise",
    "parameterise",
]
