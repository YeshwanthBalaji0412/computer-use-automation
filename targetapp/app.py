"""CoreLink Servicing Console - a deliberately hostile stand-in for legacy bank software.

Two flows:

  Flow A (read-only)  login -> member search -> results table -> member detail -> savings balance
  Flow B (write)      member detail -> open sub-account -> review -> confirm -> confirmation number

Flow B is irreversible and therefore the risky-action demo. It is also non-idempotent,
which is why the confirm step has a WRITE_TIMEOUT fault: a timeout after clicking Confirm
is genuinely ambiguous, and the correct behaviour is to escalate rather than retry.

Everything is served inside a <frameset>, laid out with nested tables, with no test ids
and control ids that churn on every render. See legacy.py for why.
"""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from targetapp import data, faults, legacy, tenants

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.globals["ctl"] = legacy.ctl
TEMPLATES.env.globals["junk"] = legacy.junk_class

SESSION_COOKIE = "corelink_sid"

#: sid -> username. In-memory; this is a fake app, not a real one.
_SESSIONS: dict[str, str] = {}

#: Keys for one-shot faults, so "session expired" and "surprise interstitial" fire once
#: and then let a retry through. A fault that fires forever cannot demonstrate recovery.
_FIRED: set[str] = set()

#: Expiries that have fired and not yet been answered by a sign-in. See `login`.
_EXPIRY_IMMUNITY = 0

app = FastAPI(title="CoreLink Servicing Console", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------- helpers


def _fire_once(key: str) -> bool:
    """True the first time for this key, False after. Lets a retry succeed."""
    if key in _FIRED:
        return False
    _FIRED.add(key)
    return True


def _authed(request: Request) -> bool:
    sid = request.cookies.get(SESSION_COOKIE)
    return bool(sid and sid in _SESSIONS)


def _render(request: Request, template: str, ctx: dict[str, Any]) -> HTMLResponse:
    tenant = ctx["tenant"]
    ctx.setdefault("modal", None)
    ctx.setdefault("banner", None)
    ctx.setdefault("rng", legacy.render_seed(secrets.token_hex(4)))
    ctx["request"] = request
    ctx["app_version"] = tenant.app_version
    return TEMPLATES.TemplateResponse(request=request, name=template, context=ctx)


def _server_error(request: Request, tenant: tenants.Tenant) -> HTMLResponse:
    resp = _render(request, "error500.html", {"tenant": tenant})
    resp.status_code = 500
    return resp


def _login_screen(
    request: Request, tenant: tenants.Tenant, *, note: str | None = None
) -> HTMLResponse:
    return _render(request, "login.html", {"tenant": tenant, "banner": note})


def _modal_for(fault: faults.Fault, sid: str, screen: str) -> dict[str, str] | None:
    """Return modal spec, or None. Two distinct modals with very different meanings."""
    if fault is faults.Fault.INTERSTITIAL and _fire_once(f"interstitial:{sid}:{screen}"):
        # Declared in the capability's `recoveries`: safe to dismiss and continue.
        return {
            "title": "Scheduled Maintenance",
            "body": "CoreLink will be unavailable Saturday 02:00-04:00 ET for scheduled "
            "maintenance. No action is required.",
            "button": "Close",
        }
    # Only on the member record, not the search list: a regulatory hold notice is a
    # property of an account, and one that appeared on every screen would be a different
    # (and less interesting) test - a run that parks repeatedly rather than once.
    if (
        fault is faults.Fault.UNKNOWN_DIALOG
        and screen == "detail"
        and _fire_once(f"unknown:{sid}:{screen}")
    ):
        # NOT declared anywhere. Replay cannot know dismissing this is safe -> escalate.
        return {
            "title": "Regulation CC Hold Notice",
            "body": "A regulatory hold has been applied to one or more accounts for this "
            "member. Review the hold before proceeding with servicing actions.",
            "button": "Acknowledge",
        }
    return None


# ---------------------------------------------------------------- auth


@app.get("/tenants/{slug}", response_class=HTMLResponse)
@app.get("/tenants/{slug}/", response_class=HTMLResponse)
async def root(request: Request, slug: str) -> Response:
    tenant = tenants.get(slug)
    if _authed(request):
        return RedirectResponse(f"/tenants/{slug}/home", status_code=302)
    return _login_screen(request, tenant)


@app.post("/tenants/{slug}/login")
async def login(
    request: Request,
    slug: str,
    username: str = Form(default=""),
    password: str = Form(default=""),
) -> Response:
    tenant = tenants.get(slug)
    if not username or not password:
        return _login_screen(request, tenant, note="User ID and password are required.")

    sid = secrets.token_hex(16)
    _SESSIONS[sid] = username
    # A session minted *after* an expiry does not immediately expire again. Without this
    # the expire fault re-arms on every new session id, so re-authenticating loops
    # forever - which is not what a timeout does in a real system, and would make a
    # correct recovery look like an unrecoverable one.
    #
    # One expiry grants exactly one immunity, consumed here. Two weaker versions of this
    # were wrong in the same way: keyed on "any fault has fired", and then on "any expiry
    # has *ever* fired", the immunity outlived the run that earned it - and because these
    # module globals live as long as the process, that silently made the outcome depend on
    # which test ran first. A pending count is the only version that is scoped to one flow.
    global _EXPIRY_IMMUNITY
    if _EXPIRY_IMMUNITY > 0:
        _EXPIRY_IMMUNITY -= 1
        _FIRED.add(f"expire:{sid}")
    resp = RedirectResponse(f"/tenants/{slug}/home", status_code=302)
    resp.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax")
    return resp


@app.get("/tenants/{slug}/logout")
async def logout(request: Request, slug: str) -> Response:
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        _SESSIONS.pop(sid, None)
    resp = RedirectResponse(f"/tenants/{slug}/", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ---------------------------------------------------------------- frameset shell


@app.get("/tenants/{slug}/home", response_class=HTMLResponse)
async def home(request: Request, slug: str) -> Response:
    """The frameset. Note there is no <body> - this is the real 1990s article."""
    tenant = tenants.get(slug)
    if not _authed(request):
        return RedirectResponse(f"/tenants/{slug}/", status_code=302)
    return TEMPLATES.TemplateResponse(
        request=request, name="frameset.html", context={"tenant": tenant}
    )


@app.get("/tenants/{slug}/frame/nav", response_class=HTMLResponse)
async def nav_frame(request: Request, slug: str) -> Response:
    tenant = tenants.get(slug)
    return _render(request, "nav.html", {"tenant": tenant})


# ---------------------------------------------------------------- content frame


@app.get("/tenants/{slug}/frame/content", response_class=HTMLResponse)
async def content_frame(
    request: Request,
    slug: str,
    screen: str = "search",
    memberId: str = "",
) -> Response:
    tenant = tenants.get(slug)
    fault = faults.current(request)
    sid = request.cookies.get(SESSION_COOKIE, "anon")

    if fault is faults.Fault.SERVER_ERROR:
        return _server_error(request, tenant)

    # Session expiry bounces the *frame* to login, which is what these apps really do.
    if fault is faults.Fault.EXPIRE and screen == "detail" and _fire_once(f"expire:{sid}"):
        _SESSIONS.pop(sid, None)
        global _EXPIRY_IMMUNITY
        _EXPIRY_IMMUNITY += 1
        return _render(
            request,
            "frame_login.html",
            {"tenant": tenant, "banner": "Your session has timed out. Please sign in again."},
        )

    if not _authed(request):
        return _render(
            request,
            "frame_login.html",
            {"tenant": tenant, "banner": "Your session has timed out. Please sign in again."},
        )

    if screen == "search":
        return _render(request, "search.html", {"tenant": tenant})

    if screen == "detail":
        if fault is faults.Fault.SLOW:
            await asyncio.sleep(6)
        return await _detail(request, tenant, memberId, sid, fault)

    if screen == "subaccount-new":
        member = data.lookup(memberId)
        if member is None:
            return _render(request, "search.html", {"tenant": tenant})
        return _render(
            request,
            "subaccount_new.html",
            {"tenant": tenant, "member": member, "types": data.SUBACCOUNT_TYPES},
        )

    return _render(request, "search.html", {"tenant": tenant})


async def _detail(
    request: Request,
    tenant: tenants.Tenant,
    member_id: str,
    sid: str,
    fault: faults.Fault,
) -> HTMLResponse:
    if data.is_restricted(member_id):
        return _render(
            request,
            "denied.html",
            {"tenant": tenant, "member_id": member_id},
        )
    member = data.lookup(member_id)
    if member is None:
        return _render(request, "notfound.html", {"tenant": tenant, "member_id": member_id})

    return _render(
        request,
        "detail.html",
        {
            "tenant": tenant,
            "member": member,
            "modal": _modal_for(fault, sid, "detail"),
        },
    )


@app.post("/tenants/{slug}/frame/search", response_class=HTMLResponse)
async def search(
    request: Request,
    slug: str,
    memberId: str = Form(default=""),
    branch: str = Form(default=""),
) -> Response:
    tenant = tenants.get(slug)
    fault = faults.current(request)
    sid = request.cookies.get(SESSION_COOKIE, "anon")

    if fault is faults.Fault.SERVER_ERROR:
        return _server_error(request, tenant)
    if not _authed(request):
        return _render(
            request,
            "frame_login.html",
            {"tenant": tenant, "banner": "Your session has timed out. Please sign in again."},
        )

    raw = memberId.strip()

    # Validation error: a business outcome, surfaced as an inline banner the way these
    # apps do it - not an HTTP error code.
    if not data.is_valid_member_id(raw):
        return _render(
            request,
            "search.html",
            {
                "tenant": tenant,
                "error": "Invalid Member ID. Enter a 6-digit numeric member number.",
                "prefill": raw,
            },
        )
    if tenant.requires_branch_on_search and not branch:
        return _render(
            request,
            "search.html",
            {
                "tenant": tenant,
                "error": "Branch selection is required.",
                "prefill": raw,
            },
        )

    if data.is_restricted(raw):
        return _render(request, "denied.html", {"tenant": tenant, "member_id": raw})

    member = data.lookup(raw)
    if member is None:
        return _render(request, "notfound.html", {"tenant": tenant, "member_id": raw})

    return _render(
        request,
        "results.html",
        {
            "tenant": tenant,
            "rows": [member],
            "modal": _modal_for(fault, sid, "results"),
        },
    )


# ---------------------------------------------------------------- flow B: write


@app.post("/tenants/{slug}/frame/subaccount/review", response_class=HTMLResponse)
async def subaccount_review(
    request: Request,
    slug: str,
    memberId: str = Form(default=""),
    nickname: str = Form(default=""),
    acctType: str = Form(default=""),
    initialDeposit: str = Form(default=""),
) -> Response:
    tenant = tenants.get(slug)
    member = data.lookup(memberId)
    if member is None:
        return _render(request, "search.html", {"tenant": tenant})

    nick = nickname.strip().upper()
    if not nick:
        return _render(
            request,
            "subaccount_new.html",
            {
                "tenant": tenant,
                "member": member,
                "types": data.SUBACCOUNT_TYPES,
                "error": "Account nickname is required.",
            },
        )

    # Pre-flight duplicate check. Makes a non-idempotent capability safe to attempt
    # twice: the second attempt returns a business outcome instead of a second account.
    if nick in data.EXISTING_SUBACCOUNTS.get(memberId, set()):
        return _render(
            request,
            "duplicate.html",
            {"tenant": tenant, "member": member, "nickname": nick},
        )

    return _render(
        request,
        "subaccount_review.html",
        {
            "tenant": tenant,
            "member": member,
            "nickname": nick,
            "acct_type": acctType or data.SUBACCOUNT_TYPES[0],
            "initial_deposit": initialDeposit or "0.00",
        },
    )


@app.post("/tenants/{slug}/frame/subaccount/confirm", response_class=HTMLResponse)
async def subaccount_confirm(
    request: Request,
    slug: str,
    memberId: str = Form(default=""),
    nickname: str = Form(default=""),
    acctType: str = Form(default=""),
) -> Response:
    tenant = tenants.get(slug)
    fault = faults.current(request)
    member = data.lookup(memberId)
    if member is None:
        return _render(request, "search.html", {"tenant": tenant})

    # The genuinely nasty one, and it has to be nasty in the right direction.
    #
    # The account is opened *first* and the error is returned after, because that is the
    # failure that actually hurts: the write committed and the acknowledgement was lost.
    # An earlier version returned the error before writing, which made the case safe and
    # the demonstration worthless - the automation said "I cannot tell whether this
    # landed" and it had not, so nothing was being prevented. Now it has, and escalating
    # is what stops a retry opening the member a second sub-account.
    #
    # Note that nothing on the screen distinguishes this from a failure that wrote
    # nothing. That is the whole point: the screen is the error, and no amount of reading
    # it settles the question. Only the system of record can.
    if fault is faults.Fault.WRITE_TIMEOUT:
        data.EXISTING_SUBACCOUNTS.setdefault(memberId, set()).add(nickname.strip().upper())
        await asyncio.sleep(3)
        return _server_error(request, tenant)

    data.EXISTING_SUBACCOUNTS.setdefault(memberId, set()).add(nickname.strip().upper())
    confirmation = f"SA-{secrets.randbelow(900000) + 100000}"
    return _render(
        request,
        "subaccount_confirmed.html",
        {
            "tenant": tenant,
            "member": member,
            "nickname": nickname.strip().upper(),
            "acct_type": acctType,
            "confirmation": confirmation,
        },
    )
