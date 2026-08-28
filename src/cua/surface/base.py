"""The surface seam.

This module is the boundary the brief asks about in 3.7: *"What's the seam between how
we perceive/act on a surface and the recorded flow?"* It is this file. Everything above
it - discovery, replay, the capability artifact - is written against these types and has
no idea whether the thing underneath is a browser, a Windows application, or a terminal.

The load-bearing constraint is what `ElementNode` **cannot** hold: there is no field for
a CSS selector, an XPath, or a DOM id. That is not an oversight. Those are the locators
that die when a legacy app re-renders (see targetapp/legacy.py), so the type system
refuses to carry them. What it carries instead is the identity a human operator uses -
a role, an accessible name, and the data context the control sits in.

Mapping to other surfaces:

    concept       web (this impl)        Windows UIA         macOS AX
    ---------     -------------------    ----------------    ------------
    role          ARIA role              ControlType         AXRole
    name          accessible name        Name                AXTitle
    frame_path    frame name chain       window/pane chain   AXWindow chain
    row_context   table th/td relation   GridItem pattern    AXRow/AXColumn
    bbox          bounding rect          BoundingRectangle   AXFrame

A `DesktopSurface` is therefore a new implementation of one ABC, not a rewrite.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum

from pydantic import BaseModel, Field

from cua.schema.common import BBox, RowContext
from cua.schema.locator import Locator, Resolution

__all__ = [
    "ActResult",
    "Action",
    "ActionType",
    "BBox",
    "ElementNode",
    "Observation",
    "RowContext",
    "Surface",
    "SurfaceError",
]


class ElementNode(BaseModel):
    """One perceivable control or piece of content."""

    ref: str = Field(
        description="Ephemeral handle, e.g. 'e14'. Valid ONLY within the observation "
        "that produced it, and never written into a capability artifact - the recorder "
        "converts the element into a durable locator ladder at the moment it is acted on."
    )
    role: str = Field(description="button | textbox | link | cell | columnheader | ...")
    name: str = Field(description="Accessible name, whitespace-normalised.")
    value: str | None = None
    states: list[str] = Field(default_factory=list, description="disabled, required, ...")
    frame_path: list[str] = Field(
        default_factory=list,
        description="Frame names from the top document down. Empty means the main frame.",
    )
    section: str | None = Field(
        default=None,
        description="Nearest preceding heading. Scopes a locator so 'Search' means "
        "'the Search button in the Member Search section'.",
    )
    row_context: RowContext | None = None
    anchor_text: str | None = Field(
        default=None,
        description="Visible text immediately preceding this element - typically the "
        "label cell in a header-less layout table. This is what makes a value readable "
        "on screens that have no table headers to key off, e.g. "
        "<td>Member ID</td><td>100042</td>.",
    )
    bbox: BBox
    tag: str = Field(default="", description="Diagnostics only. Never used for targeting.")

    def describe(self) -> str:
        """One-line human description, used in logs and intervention context."""
        bits = [f"{self.role} {self.name!r}"]
        if self.section:
            bits.append(f"in {self.section!r}")
        if self.row_context and self.row_context.row_key:
            first = next(iter(self.row_context.row_key.items()))
            bits.append(f"row {first[0]}={first[1]!r}")
        if self.frame_path:
            bits.append(f"frame {'/'.join(self.frame_path)}")
        return " ".join(bits)


class Observation(BaseModel):
    """A single perception of the surface at a point in time."""

    observation_id: str
    url: str
    title: str
    frame_paths: list[list[str]] = Field(default_factory=list)
    elements: list[ElementNode] = Field(default_factory=list)
    aria_yaml: str = Field(
        default="",
        description="Playwright's accessibility snapshot. This is what the model reads: "
        "compact, semantic, and free of the markup noise the DOM is full of.",
    )
    screenshot_path: str | None = None
    fingerprint: str = Field(
        default="",
        description="Hash of the role+name skeleton, ignoring values. Two uses: "
        "no-progress detection during discovery, drift detection during replay.",
    )
    truncated: bool = Field(
        default=False,
        description="True if the element list hit the cap and was prioritised down.",
    )

    def by_ref(self, ref: str) -> ElementNode | None:
        return next((e for e in self.elements if e.ref == ref), None)


class ActionType(StrEnum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    PRESS = "press"
    SCROLL = "scroll"


class Action(BaseModel):
    """A single interaction. Deliberately small - a bigger vocabulary means more for the
    policy engine to reason about and more that can differ between surfaces."""

    type: ActionType
    ref: str | None = Field(default=None, description="Target element, from an Observation.")
    url: str | None = None
    text: str | None = Field(default=None, description="Text to type for FILL.")
    value: str | None = Field(default=None, description="Option value/label for SELECT.")
    key: str | None = Field(default=None, description="Key name for PRESS, e.g. 'Enter'.")

    def describe(self) -> str:
        if self.type is ActionType.NAVIGATE:
            return f"navigate to {self.url}"
        detail = self.text or self.value or self.key or ""
        return f"{self.type.value} {self.ref}{f' {detail!r}' if detail else ''}"


class ActResult(BaseModel):
    ok: bool
    action: Action
    error: str | None = None
    duration_ms: int = 0
    #: True when the action caused a navigation, so callers know to re-observe.
    navigated: bool = False


class SurfaceError(RuntimeError):
    """Raised when the surface cannot carry out a request at all (element gone, frame
    detached). Distinct from a *business* failure, which is a property of what the
    application said, not of our ability to drive it."""


class Surface(ABC):
    """Perceive and act. Two methods, because everything else is a composition of them.

    Implementations are the only place in the codebase permitted to import a UI
    automation library; setup.cfg enforces that with an import-linter contract.
    """

    @abstractmethod
    async def observe(self, *, screenshot: bool = False) -> Observation:
        """Return the current state as roles, names, and data context."""

    @abstractmethod
    async def act(self, action: Action) -> ActResult:
        """Perform one interaction."""

    @abstractmethod
    async def resolve(self, locator: Locator, observation: Observation | None = None) -> Resolution:
        """Walk a locator ladder against the current state.

        Resolution belongs to the surface because the ladder's lower rungs are
        surface-specific: a desktop implementation resolves tier 4 through UIA's Grid
        pattern rather than through table markup. `Locator` itself is pure schema, so
        depending on it here does not couple this seam to any UI technology.
        """

    @abstractmethod
    async def close(self) -> None: ...
