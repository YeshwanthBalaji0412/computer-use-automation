"""Runtime fault injection.

Faults arrive on the ``X-CUA-Fault`` request header, never in the URL. That matters:
the capability artifact records URLs, so a fault expressed as a query parameter would
leak test scaffolding into a production artifact. A header lets the eval harness set
``extra_http_headers`` on the browser context and leave the recorded flow untouched.

Business outcomes (not-found, permission-denied, validation) are deliberately NOT in
here - they are driven by the member id itself, because they are ordinary application
behaviour rather than injected failures. Keeping the two mechanisms separate is the
same distinction the replay engine has to make, encoded in the target app.
"""

from __future__ import annotations

from enum import StrEnum

from fastapi import Request

HEADER = "x-cua-fault"


class Fault(StrEnum):
    NONE = "none"
    #: Detail screen takes ~6s. Recoverable: replay should wait, then succeed.
    SLOW = "slow"
    #: A "scheduled maintenance" notice the capability declares and can dismiss.
    INTERSTITIAL = "interstitial"
    #: An undeclared modal. Replay cannot know it is safe to dismiss -> escalate.
    UNKNOWN_DIALOG = "unknown-dialog"
    #: Session cookie is rejected -> bounced to login. Recoverable once via re-auth.
    EXPIRE = "expire"
    #: Server error page. Hard failure.
    SERVER_ERROR = "500"
    #: Sub-account confirm hangs and then errors. Non-idempotent + ambiguous ->
    #: must escalate, never retry, or we risk opening the account twice.
    WRITE_TIMEOUT = "write-timeout"


def current(request: Request) -> Fault:
    raw = request.headers.get(HEADER, "").strip().lower()
    if not raw:
        return Fault.NONE
    try:
        return Fault(raw)
    except ValueError:
        return Fault.NONE
