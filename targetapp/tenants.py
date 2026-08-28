"""Two tenants running the *same vendor product*, configured differently.

This is the stand-in for the real environment's "hundreds of tenants, many running the
same underlying vendor product configured, branded, and versioned differently".

The differences are deliberately the kinds that break naive automation but that a human
operator would not even notice:

  * different brand name and colours                  -> cosmetic, must not matter
  * "Search" vs "Find Member"                         -> breaks a recorded exact-name locator
  * "Member ID" vs "Member Number"                    -> breaks a recorded label locator
  * Lakeside requires a Branch selection to search    -> an extra step, not just a rename
  * balance rendered in a <table> vs a <dl>           -> breaks a recorded structural path

A capability recorded against Meridian should replay on Lakeside with a small, reviewable
overlay - not a re-recording. That is the whole multi-tenant argument, made testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Tenant:
    slug: str
    brand: str
    #: Accessible name of the search submit control.
    search_button_label: str
    #: Visible <label> text for the member identifier field.
    member_id_label: str
    #: Lakeside makes the operator pick a branch before searching.
    requires_branch_on_search: bool
    #: "table" renders the detail screen as nested tables; "dl" as a definition list.
    detail_layout: str
    accent: str
    #: Reported in the page footer. Feeds the artifact's surface fingerprint.
    app_version: str
    nav_items: list[str] = field(
        default_factory=lambda: ["Member Search", "Transactions", "Reports", "Admin"]
    )


TENANTS: dict[str, Tenant] = {
    "meridian": Tenant(
        slug="meridian",
        brand="Meridian Credit Union",
        search_button_label="Search",
        member_id_label="Member ID",
        requires_branch_on_search=False,
        detail_layout="table",
        accent="#003366",
        app_version="8.2.14",
    ),
    "lakeside": Tenant(
        slug="lakeside",
        brand="Lakeside Federal CU",
        search_button_label="Find Member",
        member_id_label="Member Number",
        requires_branch_on_search=True,
        detail_layout="dl",
        accent="#1a5c3a",
        app_version="8.4.02",
    ),
}


def get(slug: str) -> Tenant:
    try:
        return TENANTS[slug]
    except KeyError:
        raise KeyError(f"unknown tenant {slug!r}; known: {sorted(TENANTS)}") from None
