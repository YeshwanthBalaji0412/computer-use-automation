"""How a control is identified — the load-bearing decision in this whole system.

The premise, demonstrated by targetapp/legacy.py: in real legacy bank software there is
nothing stable to record. Control ids are server-generated and churn on every render,
class names are meaningless, and the table nesting shifts. Recording a selector produces
automation that works once.

What *is* stable is the identity a human operator uses: there is a button and it says
"Search"; there is a row where the Member ID column reads 100042. So a `Locator` records
an identity, and a selector is re-derived at replay time.

The ladder
----------
A locator is a *ranked list* of strategies, not a single expression. At record time we
generate every strategy that uniquely resolves; at replay time we walk them in order and
take the first that still resolves to exactly one element.

    tier 1  role + exact accessible name, scoped to a section
    tier 2  role + normalised name (case/whitespace/punctuation-insensitive)
    tier 3  associated <label> text
    tier 4  data-grid row/column  ("the Action cell in the row where Member ID = 100042")
    tier 5  text anchor            ("the cell immediately after the text 'Member ID'")
    tier 6  role ordinal within a section  (nth button in "Member Search")
    tier 7  bounding box

Three rules make this rigorous rather than a pile of fallbacks:

1. **Uniqueness is required.** A strategy matching zero or two-or-more elements is
   skipped, never guessed at. Ambiguity is an error, not a coin flip.
2. **Strategies cross-validate.** When several resolve, they must agree on the element.
   Disagreement is a drift signal worth surfacing, not something to silently resolve.
3. **The winning tier is recorded.** A step that resolved at tier 1 when recorded but
   resolves at tier 4 in production means a label changed. That telemetry is the early
   warning for UI drift, per tenant, and it costs one integer per step.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

from pydantic import BaseModel, Field

from cua.schema.common import BBox


class Tier(IntEnum):
    """Lower is better. Ordering encodes how much we trust each identity to survive."""

    ROLE_NAME_EXACT = 1
    ROLE_NAME_NORMALISED = 2
    LABEL = 3
    ROW_CELL = 4
    TEXT_ANCHOR = 5
    ROLE_ORDINAL = 6
    BBOX = 7


class NameMatch(StrEnum):
    EXACT = "exact"
    #: Case-, whitespace- and punctuation-insensitive. Absorbs the most common kind of
    #: per-tenant drift: the same control relabelled "Search" -> "Find Member".
    NORMALISED = "normalised"
    REGEX = "regex"


class LocatorStrategy(BaseModel):
    """One way to find the element. Which fields are meaningful depends on `tier`."""

    tier: Tier
    note: str = Field(
        default="",
        description="Why this strategy exists, in words. Artifacts are reviewed by "
        "humans as well as executed by machines.",
    )

    # tiers 1, 2, 6
    role: str | None = None
    name: str | None = None
    name_match: NameMatch = NameMatch.EXACT
    #: Scopes the search to a section, so "Search" cannot match a control elsewhere.
    scope_section: str | None = None
    #: tier 6 only: index among same-role elements within the scope.
    ordinal: int | None = None

    # tier 3
    label: str | None = None

    # tier 4
    row_key: dict[str, str] | None = Field(
        default=None,
        description="Column header -> expected cell text, identifying the row by data.",
    )
    column_header: str | None = None

    # tier 5
    anchor_text: str | None = Field(
        default=None,
        description="Visible text immediately preceding the target, e.g. a label cell "
        "in a header-less layout table.",
    )

    # tier 7
    bbox: BBox | None = None

    def summary(self) -> str:
        if self.tier in (Tier.ROLE_NAME_EXACT, Tier.ROLE_NAME_NORMALISED):
            scope = f" in {self.scope_section!r}" if self.scope_section else ""
            return f"{self.role} named {self.name!r} ({self.name_match}){scope}"
        if self.tier is Tier.LABEL:
            return f"control labelled {self.label!r}"
        if self.tier is Tier.ROW_CELL:
            key = ", ".join(f"{k}={v!r}" for k, v in (self.row_key or {}).items())
            return f"{self.column_header!r} cell in row where {key}"
        if self.tier is Tier.TEXT_ANCHOR:
            return f"{self.role} immediately after text {self.anchor_text!r}"
        if self.tier is Tier.ROLE_ORDINAL:
            scope = f" in {self.scope_section!r}" if self.scope_section else ""
            return f"{self.role} #{self.ordinal}{scope}"
        return f"box at ({self.bbox.x:.0f}, {self.bbox.y:.0f})" if self.bbox else "bbox"


class Locator(BaseModel):
    """A ranked identity for one control."""

    describe: str = Field(
        description="Human-readable target, e.g. 'the Search button in the Member "
        "Search form'. Written for a reviewer approving a capability, not for a machine."
    )
    frame_path: list[str] = Field(
        default_factory=list,
        description="Frame names from the top document down. Empty means the main "
        "frame. On a desktop surface this becomes a window/pane path.",
    )
    any_frame: bool = Field(
        default=False,
        description="Match in whichever frame the element happens to be in, ignoring "
        "`frame_path`. Recorded flows always pin the frame - that is part of the "
        "element's identity. Platform-level recoveries do not: a maintenance notice can "
        "surface in the content frame on one screen and the main document on another, "
        "and a recovery that only looked in one would silently fail to dismiss it.",
    )
    strategies: list[LocatorStrategy] = Field(
        default_factory=list,
        description="Ranked best-first. Replay takes the first that uniquely resolves.",
    )

    @property
    def best_tier(self) -> Tier | None:
        return min((s.tier for s in self.strategies), default=None)

    @property
    def control_name(self) -> str:
        """The accessible name this locator targets, if it has one.

        Risk is judged from what a control *says*, so this is what the risk classifier
        reads. Taking it from the artifact rather than from the live page means the
        classification a reviewer sees at approval time is the one the runtime applies -
        and it is available before the locator is resolved, which is required because
        policy is checked before anything touches the page.
        """
        for strategy in sorted(self.strategies, key=lambda s: s.tier):
            if strategy.name:
                return strategy.name
            if strategy.label:
                return strategy.label
        return ""

    def explain(self) -> str:
        lines = [f"{self.describe}  [frame: {'/'.join(self.frame_path) or 'main'}]"]
        lines += [f"  tier {s.tier}: {s.summary()}" for s in self.strategies]
        return "\n".join(lines)


class ResolveOutcome(StrEnum):
    RESOLVED = "resolved"
    #: No strategy matched anything. The control is gone, or the screen is wrong.
    NOT_FOUND = "not_found"
    #: Something matched, but more than one element. Never guess - this is a failure.
    AMBIGUOUS = "ambiguous"


class Resolution(BaseModel):
    """The result of walking the ladder. Carries the evidence for the drift signal."""

    outcome: ResolveOutcome
    ref: str | None = Field(default=None, description="Surface handle, when resolved.")
    winning_tier: Tier | None = None
    tiers_tried: list[Tier] = Field(default_factory=list)
    #: Tiers that resolved uniquely but to a *different* element than the winner.
    disagreeing_tiers: list[Tier] = Field(default_factory=list)
    detail: str = ""

    @property
    def degraded(self) -> bool:
        """True when a better-ranked strategy was tried and failed.

        Not a failure - the step still ran. It is the signal that something about this
        screen changed, which is exactly what you want to know per tenant before it
        becomes an outage.
        """
        if self.winning_tier is None or not self.tiers_tried:
            return False
        return min(self.tiers_tried) < self.winning_tier
