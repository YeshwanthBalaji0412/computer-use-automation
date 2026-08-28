"""Playwright implementation of `Surface`.

The only module in the system that imports Playwright. An import-linter contract in
setup.cfg enforces that, which is what makes the "swap in a DesktopSurface" story a
structural claim rather than an aspiration.

Determinism choices worth defending
-----------------------------------
The browser context is pinned: fixed viewport, fixed locale and timezone, animations
disabled. Those four settings remove an entire class of replay flake before any wait
logic is written - a run that renders identically every time cannot fail because a
transition was mid-flight or a date formatted differently.

There is not a single `sleep()` in this file. Every wait is a condition with a timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Frame,
    Page,
    Playwright,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from cua.locator.match import resolve as match_resolve
from cua.schema.locator import Locator, Resolution
from cua.surface.base import (
    Action,
    ActionType,
    ActResult,
    BBox,
    ElementNode,
    Observation,
    RowContext,
    Surface,
    SurfaceError,
)
from cua.surface.snapshot import COLLECT_JS, priority

#: Cap on elements handed upstream. A 30-step discovery run that ships an unbounded tree
#: every turn spends most of its budget on markup nobody reads.
MAX_ELEMENTS = 120

#: Bounded, and short: these are conditions, not sleeps.
SETTLE_TIMEOUT_MS = 5_000
FRAME_SETTLE_MS = 2_000
ACTION_TIMEOUT_MS = 10_000

#: How long to let a navigation *begin* after an action that might cause one. Only paid
#: by actions that can navigate, and only in full when none happens.
NAV_GRACE_S = 0.4

#: Placeholder substituted for any secret found while building the model-facing snapshot.
REDACTED_SECRET = "«redacted:password»"

#: Actions that can trigger a navigation. FILL is excluded - typing into a field does not
#: submit it, and paying the grace period on every keystroke-equivalent adds up.
_MAY_NAVIGATE = frozenset({ActionType.CLICK, ActionType.PRESS, ActionType.SELECT})


class WebSurface(Surface):
    def __init__(
        self,
        page: Page,
        context: BrowserContext,
        *,
        max_elements: int = MAX_ELEMENTS,
        evidence_dir: Path | None = None,
    ) -> None:
        self._page = page
        self._context = context
        self._max_elements = max_elements
        self._evidence_dir = evidence_dir
        #: global ref -> (frame, per-frame ref stamped in the DOM)
        self._refs: dict[str, tuple[Frame, str]] = {}

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    async def launch(
        cls,
        *,
        headed: bool = False,
        evidence_dir: Path | None = None,
        extra_http_headers: dict[str, str] | None = None,
        allow_request: Callable[[str], bool] | None = None,
        on_blocked_request: Callable[[str], None] | None = None,
    ) -> tuple[WebSurface, Playwright, Browser]:
        """Launch a pinned browser context.

        `allow_request` installs the **network-level** half of the guardrail model. It
        is a plain callable rather than a PolicyEngine so this adapter stays decoupled
        from the policy package; the composition root wires the two together.

        This layer is not redundant with action-level checks. Those govern what the
        agent chooses to do; this governs what the *page* does on the agent's behalf -
        a 302 to an external identity provider, or a beacon injected into a legacy app's
        free-text field. Neither involves an action, so only this catches them.
        """
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=not headed)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            reduced_motion="reduce",
            extra_http_headers=extra_http_headers or {},
        )
        context.set_default_timeout(ACTION_TIMEOUT_MS)

        if allow_request is not None:

            async def _guard(route: Route) -> None:
                url = route.request.url
                if allow_request(url):
                    await route.continue_()
                else:
                    if on_blocked_request is not None:
                        on_blocked_request(url)
                    await route.abort("blockedbyclient")

            await context.route("**/*", _guard)

        page = await context.new_page()
        return cls(page, context, evidence_dir=evidence_dir), pw, browser

    async def close(self) -> None:
        await self._context.close()

    @property
    def page(self) -> Page:
        """Escape hatch for the control layer, which needs the live page for CDP
        screencasting during a human handoff. Deliberately not part of `Surface`."""
        return self._page

    # ------------------------------------------------------------------ perceive

    async def observe(self, *, screenshot: bool = False) -> Observation:
        await self._settle()

        self._refs.clear()
        collected: list[ElementNode] = []
        frame_paths: list[list[str]] = []
        aria_parts: list[str] = []
        seq = 0

        for frame in self._page.frames:
            if frame.is_detached():
                continue
            path = _frame_path(frame)
            frame_paths.append(path)

            try:
                raw: list[dict[str, Any]] = await frame.evaluate(COLLECT_JS)
            except PlaywrightError:
                # A frame can detach or navigate mid-walk. Skipping it is correct: the
                # next observation will pick it up, and a partial view is better than
                # failing the whole perception pass.
                continue

            for item in raw:
                node = _to_node(item, ref=f"e{seq}", frame_path=path)
                self._refs[node.ref] = (frame, str(item["ref"]))
                collected.append(node)
                seq += 1

            aria = await _aria_snapshot(frame)
            if aria:
                label = "/".join(path) or "main"
                aria_parts.append(f"# frame: {label}\n{aria}")

        elements, truncated = _apply_budget(collected, self._max_elements)

        shot: str | None = None
        if screenshot and self._evidence_dir is not None:
            self._evidence_dir.mkdir(parents=True, exist_ok=True)
            target = self._evidence_dir / f"obs-{uuid.uuid4().hex[:8]}.png"
            await self._page.screenshot(path=str(target))
            shot = str(target)

        return Observation(
            observation_id=f"obs_{uuid.uuid4().hex[:12]}",
            url=self._page.url,
            title=await self._page.title(),
            frame_paths=frame_paths,
            elements=elements,
            aria_yaml="\n\n".join(aria_parts),
            screenshot_path=shot,
            fingerprint=_fingerprint(elements),
            truncated=truncated,
        )

    async def resolve(self, locator: Locator, observation: Observation | None = None) -> Resolution:
        """Resolve a locator against the live surface.

        Resolution runs against a fresh `Observation` rather than against Playwright
        selectors. That is deliberate: the same pure matcher is used at record time and
        at replay time, so a strategy recorded because it uniquely matched cannot later
        be resolved by different rules and pick a different element. It also expresses
        relational queries a selector engine cannot - "the Action cell in the row where
        Member ID = 100042" is not CSS.
        """
        obs = observation or await self.observe()
        return match_resolve(locator, obs)

    # ------------------------------------------------------------------ act

    async def act(self, action: Action) -> ActResult:
        started = time.monotonic()
        before = self._url_snapshot()

        navigated = asyncio.Event()

        def _on_nav(_frame: Frame) -> None:
            navigated.set()

        # Registered per action and removed in `finally`: a listener left behind on every
        # act would accumulate across a 30-step run and fire for unrelated navigations.
        self._page.on("framenavigated", _on_nav)
        try:
            try:
                await self._dispatch(action)
            except PlaywrightTimeout as exc:
                return self._failed(action, started, f"timed out: {exc}")
            except (PlaywrightError, SurfaceError) as exc:
                # SurfaceError is ours (stale ref, detached frame). A failed action, not
                # a crash: the caller re-observes and retries rather than unwinding.
                return self._failed(action, started, str(exc).splitlines()[0])

            if action.type in _MAY_NAVIGATE:
                # Playwright's click returns once the event is dispatched - before the
                # request has even been issued - so `networkidle` below would observe a
                # quiet network and return at once. Wait, briefly and boundedly, for the
                # navigation to *start*; `_settle` then waits for it to finish.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(navigated.wait(), timeout=NAV_GRACE_S)
        finally:
            self._page.remove_listener("framenavigated", _on_nav)

        await self._settle()
        return ActResult(
            ok=True,
            action=action,
            duration_ms=int((time.monotonic() - started) * 1000),
            navigated=self._url_snapshot() != before,
        )

    async def _dispatch(self, action: Action) -> None:
        if action.type is ActionType.NAVIGATE:
            if not action.url:
                raise SurfaceError("navigate requires a url")
            await self._page.goto(action.url, wait_until="domcontentloaded")
            return

        if action.type is ActionType.SCROLL:
            await self._page.mouse.wheel(0, 400)
            return

        locator = self._locate(action)

        if action.type is ActionType.CLICK:
            await locator.click(timeout=ACTION_TIMEOUT_MS)
        elif action.type is ActionType.FILL:
            await locator.fill(action.text or "", timeout=ACTION_TIMEOUT_MS)
        elif action.type is ActionType.PRESS:
            await locator.press(action.key or "Enter", timeout=ACTION_TIMEOUT_MS)
        elif action.type is ActionType.SELECT:
            value = action.value or ""
            try:
                await locator.select_option(value=value, timeout=ACTION_TIMEOUT_MS)
            except PlaywrightError:
                # Legacy selects often carry a display label that differs from the value.
                await locator.select_option(label=value, timeout=ACTION_TIMEOUT_MS)
        else:  # pragma: no cover - ActionType is exhaustive
            raise SurfaceError(f"unsupported action {action.type}")

    def _locate(self, action: Action):  # type: ignore[no-untyped-def]
        if not action.ref:
            raise SurfaceError(f"{action.type.value} requires a ref")
        entry = self._refs.get(action.ref)
        if entry is None:
            raise SurfaceError(
                f"unknown ref {action.ref!r}; refs are only valid for the observation "
                f"that produced them - re-observe before acting"
            )
        frame, dom_ref = entry
        if frame.is_detached():
            raise SurfaceError(f"frame for ref {action.ref!r} detached; re-observe")
        return frame.locator(f'[data-cua-ref="{dom_ref}"]')

    @staticmethod
    def _failed(action: Action, started: float, error: str) -> ActResult:
        return ActResult(
            ok=False,
            action=action,
            error=error,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # ------------------------------------------------------------------ internals

    def _url_snapshot(self) -> tuple[str, ...]:
        """Frame URLs, not just the page URL.

        The target app navigates the *content frame* on most interactions, which leaves
        the top-level URL untouched. Anything watching only `page.url` would conclude
        nothing happened - the same trap that makes session-expiry detection hard.
        """
        return tuple(f.url for f in self._page.frames if not f.is_detached())

    async def _settle(self) -> None:
        """Wait for the page *and every frame* to stop moving.

        Waiting only on the main page is wrong for a frameset app: the top-level document
        never navigates, so `page.wait_for_load_state` returns instantly while the content
        frame is still mid-request. `networkidle` accounts for subframe traffic, and the
        per-frame pass covers a frame that finished loading after the page went quiet.

        Timeouts are swallowed on purpose. A slow load is a *condition* for the state
        classifier to interpret - recoverable, most likely - not an error for the surface
        to raise. Perception reports what is there; it does not adjudicate.
        """
        with contextlib.suppress(PlaywrightTimeout):
            await self._page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
        for frame in self._page.frames:
            if frame.is_detached():
                continue
            with contextlib.suppress(PlaywrightTimeout, PlaywrightError):
                await frame.wait_for_load_state("domcontentloaded", timeout=FRAME_SETTLE_MS)


# ---------------------------------------------------------------------- helpers


def _frame_path(frame: Frame) -> list[str]:
    """Frame names from the top document down. Empty list means the main frame."""
    path: list[str] = []
    node: Frame | None = frame
    while node is not None and node.parent_frame is not None:
        path.append(node.name or "<unnamed>")
        node = node.parent_frame
    return list(reversed(path))


#: Playwright emits `/url: ...` lines for links. Legacy postback hrefs embed the very
#: control ids that churn on every render, so leaving them in would put an unstable
#: identifier in front of the model and invite it to build a locator from one.
_URL_LINE = re.compile(r"^\s*-?\s*/url:.*$", re.MULTILINE)


async def _aria_snapshot(frame: Frame) -> str:
    """The model-facing view of a frame: roles, names, values - nothing else.

    Two things are stripped before it leaves this function. URLs, because they carry
    volatile ids. And password values, because the aria snapshot renders input values
    verbatim and this string goes into an LLM prompt. The Redactor is the general
    mechanism for the second one, but perception should not emit a secret in the first
    place - defence in depth on the path that leaves the machine.
    """
    snap = ""
    for selector in ("body", "html"):
        try:
            snap = await frame.locator(selector).first.aria_snapshot(timeout=SETTLE_TIMEOUT_MS)
        except PlaywrightError:
            continue
        if snap and snap.strip():
            break
    if not snap or not snap.strip():
        return ""

    snap = _URL_LINE.sub("", snap)

    try:
        secrets_present: list[str] = await frame.evaluate(
            "() => Array.from(document.querySelectorAll('input[type=password]'))"
            ".map(e => e.value).filter(v => v && v.length)"
        )
    except PlaywrightError:
        secrets_present = []
    for secret in secrets_present:
        snap = snap.replace(secret, REDACTED_SECRET)

    return "\n".join(line for line in snap.splitlines() if line.strip()).strip()


def _to_node(item: dict[str, Any], *, ref: str, frame_path: list[str]) -> ElementNode:
    rc = item.get("row_context")
    return ElementNode(
        ref=ref,
        role=str(item["role"]),
        name=str(item.get("name") or ""),
        value=item.get("value"),
        states=list(item.get("states") or []),
        frame_path=frame_path,
        section=item.get("section"),
        row_context=RowContext(**rc) if rc else None,
        anchor_text=item.get("anchor_text"),
        bbox=BBox(**item["bbox"]),
        tag=str(item.get("tag") or ""),
    )


def _apply_budget(nodes: list[ElementNode], cap: int) -> tuple[list[ElementNode], bool]:
    """Trim to the budget by dropping low-value content first, preserving document order.

    Interactive controls are kept ahead of text: an agent that cannot see a button cannot
    act at all, whereas losing the tail of a long table is survivable.
    """
    if len(nodes) <= cap:
        return nodes, False

    ranked = sorted(enumerate(nodes), key=lambda pair: (priority(pair[1].role), pair[0]))[:cap]
    kept = [node for _, node in sorted(ranked, key=lambda pair: pair[0])]
    return kept, True


def _fingerprint(nodes: list[ElementNode]) -> str:
    """Hash the *shape* of the screen - roles, names, frames - but not values.

    Ignoring values is deliberate. Typing into a field must not change the fingerprint,
    or no-progress detection would never fire; but a different screen must, or drift
    detection would never fire.
    """
    skeleton = "\n".join(f"{'/'.join(n.frame_path)}|{n.role}|{n.name}" for n in nodes)
    return hashlib.sha256(skeleton.encode()).hexdigest()[:16]
