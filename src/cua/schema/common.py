"""Value types shared by perception and the artifact schema.

`cua.schema` is the bottom layer: it imports nothing else from the application, which an
import-linter contract enforces. Everything else may depend on it. That direction matters
because the artifact schema is the contract the whole system is organised around - if it
depended on the Playwright adapter, "swap the surface" would be a lie.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class BBox(BaseModel):
    """Viewport coordinates.

    Used for screenshots and redaction masking, and - only as the last rung of the
    locator ladder - for coordinate clicking on surfaces that expose nothing better
    (Citrix, canvas apps, remote desktop). Recorded so the abstraction is not a lie
    about those cases; ranked last so it is never reached when anything better resolves.
    """

    x: float
    y: float
    w: float
    h: float


class RowContext(BaseModel):
    """Where a control sits inside a data grid.

    This is what makes it possible to say "the View link in the row whose Member ID cell
    reads 100042" instead of "the third link on the page". Both row and column are
    addressed by *data*, so the reference survives reordering, extra rows, and the
    churning control ids legacy grids emit.

    Populated only for genuine data grids - tables with header cells in their own row.
    Layout tables, which legacy apps use for positioning, deliberately produce nothing:
    inventing column names from a header-less table yields locators that look precise
    and are not.
    """

    row_key: dict[str, str] = Field(
        description="Column header -> cell text for this element's row.",
    )
    column_header: str = Field(
        default="",
        description="Header of the column this element is in.",
    )
