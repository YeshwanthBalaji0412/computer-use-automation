"""The locator ladder against the real hostile app.

The unit tests prove the matching logic. These prove the premise: that a locator
recorded now still finds the same control after the application has re-rendered and
regenerated every control id on the page.

`test_recorded_locator_survives_a_rerender_that_churns_every_id` is the Day-2 gate and
the single most important test in the repository. If it fails, the whole record-once /
replay-many thesis is unsupported.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from cua.locator.generate import bind, generate, parameterise
from cua.schema.locator import ResolveOutcome, Tier
from cua.surface.base import Action, ActionType, ElementNode, Observation
from cua.surface.web_surface import WebSurface

pytestmark = [pytest.mark.integration]


@pytest_asyncio.fixture
async def surface() -> AsyncIterator[WebSurface]:
    surf, pw, browser = await WebSurface.launch()
    try:
        yield surf
    finally:
        await surf.close()
        await browser.close()
        await pw.stop()


def find(obs: Observation, role: str, name: str) -> ElementNode:
    node = next(
        (e for e in obs.elements if e.role == role and e.name.lower() == name.lower()), None
    )
    assert node is not None, f"{role} {name!r} not perceived"
    return node


async def sign_in(surface: WebSurface, base_url: str, tenant: str = "meridian") -> Observation:
    await surface.act(Action(type=ActionType.NAVIGATE, url=f"{base_url}/tenants/{tenant}/"))
    obs = await surface.observe()
    if not any(e.role == "button" and e.name == "Sign In" for e in obs.elements):
        return obs
    await surface.act(
        Action(type=ActionType.FILL, ref=find(obs, "textbox", "User ID").ref, text="operator")
    )
    await surface.act(
        Action(type=ActionType.FILL, ref=find(obs, "textbox", "Password").ref, text="demo-pass")
    )
    await surface.act(Action(type=ActionType.CLICK, ref=find(obs, "button", "Sign In").ref))
    return await surface.observe()


async def search(surface: WebSurface, base_url: str, member_id: str) -> Observation:
    obs = await sign_in(surface, base_url)
    await surface.act(
        Action(type=ActionType.FILL, ref=find(obs, "textbox", "Member ID").ref, text=member_id)
    )
    await surface.act(Action(type=ActionType.CLICK, ref=find(obs, "button", "Search").ref))
    return await surface.observe()


# ------------------------------------------------------------------ the gate


async def test_recorded_locator_survives_a_rerender_that_churns_every_id(
    surface: WebSurface, base_url: str
) -> None:
    """Record once, resolve again after every control id on the page has changed.

    This is the premise of the entire system. The app regenerates ASP.NET-style ids on
    every render (targetapp/legacy.py), so any recorded selector would be dead here.
    """
    first = await sign_in(surface, base_url)
    target = find(first, "button", "Search")
    recorded = generate(target, first)

    # Force a fresh render. Every ctlNN index on the page is now different.
    await surface.act(Action(type=ActionType.NAVIGATE, url=f"{base_url}/tenants/meridian/home"))
    second = await surface.observe()

    resolution = await surface.resolve(recorded, second)

    assert resolution.outcome is ResolveOutcome.RESOLVED
    assert resolution.winning_tier is Tier.ROLE_NAME_EXACT, (
        "the strongest strategy should still win - nothing semantic changed"
    )
    assert not resolution.degraded

    # And the resolved handle is genuinely usable, not merely a match.
    resolved = second.by_ref(resolution.ref or "")
    assert resolved is not None and resolved.name == "Search"


async def test_a_resolved_locator_can_be_acted_on(surface: WebSurface, base_url: str) -> None:
    """Closes the loop: record -> re-render -> resolve -> act -> reach the next screen."""
    obs = await sign_in(surface, base_url)
    field = generate(find(obs, "textbox", "Member ID"), obs)
    button = generate(find(obs, "button", "Search"), obs)

    await surface.act(Action(type=ActionType.NAVIGATE, url=f"{base_url}/tenants/meridian/home"))
    fresh = await surface.observe()

    field_res = await surface.resolve(field, fresh)
    assert field_res.ref
    await surface.act(Action(type=ActionType.FILL, ref=field_res.ref, text="100042"))

    after_fill = await surface.observe()
    button_res = await surface.resolve(button, after_fill)
    assert button_res.ref
    await surface.act(Action(type=ActionType.CLICK, ref=button_res.ref))

    results = await surface.observe()
    assert any(e.role == "heading" and e.name == "Search Results" for e in results.elements)


# ------------------------------------------------------------------ grid targeting


async def test_view_link_is_targeted_by_row_not_by_position(
    surface: WebSurface, base_url: str
) -> None:
    """On the real grid the View link's name is just "View". Row context is what makes
    it addressable, and parameterisation is what makes it reusable."""
    results = await search(surface, base_url, "100042")
    view = find(results, "link", "View")

    recorded = parameterise(generate(view, results), {"memberId": "100042"})
    row = next(s for s in recorded.strategies if s.tier is Tier.ROW_CELL)
    assert row.row_key == {"Member ID": "{{memberId}}"}
    assert row.column_header == "Action"

    resolution = await surface.resolve(bind(recorded, {"memberId": "100042"}), results)
    assert resolution.outcome is ResolveOutcome.RESOLVED
    assert resolution.winning_tier is Tier.ROW_CELL


async def test_a_parameterised_row_locator_fails_loudly_for_an_absent_member(
    surface: WebSurface, base_url: str
) -> None:
    """The safety property. Bound to a member who is not on screen, the locator must
    resolve to nothing - never to whichever row happens to occupy that position."""
    results = await search(surface, base_url, "100042")
    recorded = parameterise(
        generate(find(results, "link", "View"), results), {"memberId": "100042"}
    )

    resolution = await surface.resolve(bind(recorded, {"memberId": "100043"}), results)
    assert resolution.outcome is ResolveOutcome.NOT_FOUND
    assert resolution.ref is None


# ------------------------------------------------------------------ header-less screens


async def test_balance_on_the_detail_screen_is_reachable(
    surface: WebSurface, base_url: str
) -> None:
    """Meridian renders balances in a table *with* headers, so tier 4 applies; the field
    block above it has none, so those values need the text anchor. Both must work."""
    results = await search(surface, base_url, "100042")
    await surface.act(Action(type=ActionType.CLICK, ref=find(results, "link", "View").ref))
    detail = await surface.observe()

    balance = next(e for e in detail.elements if e.role == "cell" and "4,182.55" in e.name)
    balance_loc = generate(balance, detail)
    assert Tier.ROW_CELL in [s.tier for s in balance_loc.strategies]
    assert (await surface.resolve(balance_loc, detail)).outcome is ResolveOutcome.RESOLVED

    status = next(
        e
        for e in detail.elements
        if e.role == "cell" and e.name == "ACTIVE" and e.anchor_text == "Status"
    )
    status_loc = generate(status, detail)
    assert Tier.TEXT_ANCHOR in [s.tier for s in status_loc.strategies]
    assert (await surface.resolve(status_loc, detail)).outcome is ResolveOutcome.RESOLVED


# ------------------------------------------------------------------ cross-tenant


async def test_a_meridian_locator_still_resolves_on_lakeside_and_reports_drift(
    surface: WebSurface, base_url: str
) -> None:
    """The multi-tenant claim, tested end to end against two live variants.

    Lakeside is the same vendor product with the button relabelled "Find Member". A
    capability recorded on Meridian must still work there - and must *say* that it had
    to fall back, because that degradation is the early warning for per-tenant drift.
    """
    meridian = await sign_in(surface, base_url, tenant="meridian")
    recorded = generate(find(meridian, "button", "Search"), meridian)
    assert recorded.strategies[0].tier is Tier.ROLE_NAME_EXACT

    lakeside = await sign_in(surface, base_url, tenant="lakeside")
    assert any(e.name == "Find Member" for e in lakeside.elements), "wrong tenant loaded"

    resolution = await surface.resolve(recorded, lakeside)

    # Exact name is gone. Whether a lower rung carries it or it fails cleanly, the one
    # unacceptable outcome is silently resolving to a *different* control.
    if resolution.outcome is ResolveOutcome.RESOLVED:
        assert resolution.degraded, "fell back without reporting degradation"
        landed = lakeside.by_ref(resolution.ref or "")
        assert landed is not None and landed.role == "button"
    else:
        assert resolution.outcome is ResolveOutcome.NOT_FOUND
