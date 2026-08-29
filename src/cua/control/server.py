"""The operator console.

A full real-time co-browsing console is out of scope per the brief; what has to be real
is *"the handoff mechanism and the control-transfer model"*. So this is deliberately
small - one page, four buttons, a screencast - and the parts that carry weight are:

* the intervention payload an operator receives is the same object a queue would deliver
* taking control transfers the **lease**, so automation cannot act while they hold it
* the operator drives the *same* `BrowserContext` the run was using, not a fresh one
* resuming states a disposition, and automation re-orients from the screen rather than
  trusting it

The live view uses the Chrome DevTools Protocol: `Page.startScreencast` pushes JPEG
frames, and operator input is replayed with `Input.dispatchMouseEvent` /
`dispatchKeyEvent`. That means an operator needs nothing but a browser tab - no VNC, no
shared desktop, and it works when the run is headless on a server, which is the shape
this takes in production.

Production would add the parts that are pure plumbing here: operator identity and authz
on every route, per-tenant routing and SLAs, and multiple concurrent sessions. The seam
does not change - only who is allowed through it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from cua.control.intervention import InterventionStore
from cua.control.session import Disposition, HumanAction, SessionController

CONSOLE_HTML = Path(__file__).parent / "console.html"


def build_app(
    *,
    store: InterventionStore,
    controller: SessionController,
    page_provider: Any,
) -> FastAPI:
    """`page_provider` is a zero-arg callable returning the live Playwright Page.

    Passed as a callable rather than a Page so this module never imports Playwright and
    stays inside the architecture contract - and so the console can be started before a
    run has a page.
    """
    app = FastAPI(title="Operator console", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(CONSOLE_HTML.read_text(encoding="utf-8"))

    @app.get("/interventions/{intervention_id}", response_class=HTMLResponse)
    async def detail(intervention_id: str) -> HTMLResponse:
        return HTMLResponse(CONSOLE_HTML.read_text(encoding="utf-8"))

    @app.get("/api/state")
    async def state() -> JSONResponse:
        return JSONResponse(
            {
                "owner": controller.owner,
                "epoch": controller.epoch,
                "awaiting_human": controller.awaiting_human,
                "human_actions": [a.describe() for a in controller.human_actions],
                "interventions": [i.model_dump(mode="json") for i in store.all_items()],
            }
        )

    @app.post("/api/interventions/{intervention_id}/take")
    async def take(intervention_id: str) -> JSONResponse:
        item = store.take(intervention_id, operator="operator")
        if item is None:
            return JSONResponse({"error": "not open"}, status_code=409)
        # The lease already moved to `human` when automation ceded; this records who
        # picked it up. Automation is parked on a future either way.
        return JSONResponse({"ok": True, "owner": controller.owner})

    @app.post("/api/interventions/{intervention_id}/resume")
    async def resume(intervention_id: str, body: dict[str, str]) -> JSONResponse:
        raw = body.get("disposition", Disposition.RECOVERED)
        try:
            disposition = Disposition(raw)
        except ValueError:
            return JSONResponse({"error": f"unknown disposition {raw!r}"}, status_code=400)

        if not controller.awaiting_human:
            return JSONResponse({"error": "nothing is waiting to resume"}, status_code=409)

        handoff = controller.resume(disposition, note=body.get("note", ""))
        return JSONResponse(
            {
                "ok": True,
                "owner": controller.owner,
                "epoch": controller.epoch,
                "disposition": str(handoff.disposition),
                "human_actions": [a.describe() for a in handoff.actions],
            }
        )

    @app.websocket("/api/stream")
    async def stream(socket: WebSocket) -> None:
        """Live view of the session, plus operator input, over one socket.

        Frames out, clicks and keystrokes in. The operator is driving the actual page
        the run was using, which is what "take control of the live session - not a
        fresh one" means in practice.
        """
        await socket.accept()
        page = page_provider()
        if page is None:
            await socket.send_json({"type": "error", "message": "no live session"})
            await socket.close()
            return

        session = await page.context.new_cdp_session(page)
        frames: asyncio.Queue[str] = asyncio.Queue(maxsize=2)

        def on_frame(event: dict[str, Any]) -> None:
            with contextlib.suppress(asyncio.QueueFull):
                frames.put_nowait(event["data"])
            # Acknowledging is mandatory: without it Chrome stops sending after one
            # frame and the view silently freezes.
            asyncio.create_task(  # noqa: RUF006
                session.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
            )

        session.on("Page.screencastFrame", on_frame)
        await session.send(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 70, "maxWidth": 1280, "maxHeight": 900},
        )

        async def pump() -> None:
            while True:
                data = await frames.get()
                await socket.send_json({"type": "frame", "data": data})

        pumping = asyncio.create_task(pump())
        try:
            while True:
                message = await socket.receive_json()
                await _dispatch_input(session, controller, message)
        except WebSocketDisconnect:
            pass
        finally:
            pumping.cancel()
            with contextlib.suppress(Exception):
                await session.send("Page.stopScreencast")
            with contextlib.suppress(Exception):
                await session.detach()

    return app


async def _dispatch_input(
    session: Any, controller: SessionController, message: dict[str, Any]
) -> None:
    """Replay an operator's input into the live page.

    Refused unless the human actually holds the lease. Without that check the console
    would be a way to reach into a session automation is still driving, which is exactly
    the race the lease exists to prevent - in the other direction.
    """
    if controller.owner != "human":
        return

    kind = message.get("type")
    if kind == "mouse":
        for phase in ("mousePressed", "mouseReleased"):
            await session.send(
                "Input.dispatchMouseEvent",
                {
                    "type": phase,
                    "x": message.get("x", 0),
                    "y": message.get("y", 0),
                    "button": "left",
                    "clickCount": 1,
                },
            )
        controller.record_human_action(
            HumanAction(at=_now(), kind="click", detail=f"({message.get('x')}, {message.get('y')})")
        )
    elif kind == "key":
        text = message.get("text", "")
        await session.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "key": message.get("key", ""),
                "text": text if len(text) == 1 else "",
            },
        )
        await session.send(
            "Input.dispatchKeyEvent", {"type": "keyUp", "key": message.get("key", "")}
        )
    elif kind == "scroll":
        await session.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": message.get("x", 0),
                "y": message.get("y", 0),
                "deltaX": 0,
                "deltaY": message.get("deltaY", 0),
            },
        )


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


def decode_frame(data: str) -> bytes:
    """Screencast frames arrive base64-encoded; used when saving one as evidence."""
    return base64.b64decode(data)
