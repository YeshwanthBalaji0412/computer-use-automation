"""Who is driving.

The brief asks for a seam where *"automation must be able to pause, cede control, and
resume on the same session, and there must be a way to know who is (or should be) in
control."* This is that seam, and it is a **lease** rather than a flag.

Three properties, and each one is doing work:

**Exclusivity is enforced, not requested.** While a human holds the lease, an automation
action does not politely wait - it raises. `WebSurface` is constructed with a guard that
calls `assert_owner` before every act, so the prohibition lives below anything that might
forget to check it.

**An epoch counter invalidates in-flight work.** Automation is asynchronous: an action can
be mid-`await` when a human takes over. Without a monotonic epoch, that action lands
*after* the transfer, on a page the operator is now editing. The epoch makes a stale
action a loud error instead of a silent corruption. This is the real race in the design,
and a boolean cannot express it.

**The session is never torn down.** Ceding parks a coroutine on a future; it does not
close the browser. Cookies, auth, scroll position, a half-filled form and the frame tree
all survive, because it is literally the same page object. "Take control of the live
session - not a fresh one" is the requirement, and this is what makes it true rather
than approximately true.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

Controller = Literal["automation", "human", "none"]


class Disposition(StrEnum):
    """What the operator says they did, which decides how automation re-orients."""

    #: The operator performed this step. Move on to the next one.
    STEP_COMPLETED = "step_completed"
    #: The operator cleared the obstacle. Try the step again.
    RECOVERED = "recovered"
    #: The operator finished the whole flow by hand.
    FLOW_COMPLETED = "flow_completed"
    #: Stop. Do not continue.
    ABORT = "abort"


class ControlLeaseViolation(RuntimeError):  # noqa: N818 - reads better without a suffix
    """Automation tried to act while a human held the lease."""

    def __init__(self, held_by: Controller, attempted_by: Controller) -> None:
        super().__init__(f"{attempted_by} attempted to act while {held_by} holds the control lease")
        self.held_by = held_by
        self.attempted_by = attempted_by


class StaleEpochError(RuntimeError):
    """An action begun before a control transfer tried to land after it."""

    def __init__(self, expected: int, got: int) -> None:
        super().__init__(
            f"action carries control epoch {got}, but the session is at {expected}; "
            f"control changed hands while this action was in flight"
        )
        self.expected = expected
        self.got = got


@dataclass
class HumanAction:
    """Something the operator did while holding the lease.

    Recorded in the same evidence stream as automation's own actions, distinguished only
    by `actor`. The requirement is to "record what the human did"; keeping one stream
    means the handoff reads as a continuous narrative rather than two logs to correlate.
    """

    at: datetime
    kind: str
    role: str = ""
    name: str = ""
    frame_path: str = ""
    detail: str = ""

    def describe(self) -> str:
        target = f"{self.role} {self.name!r}".strip() if self.role else self.detail
        return f"{self.kind} {target}".strip()


@dataclass
class HandoffResult:
    disposition: Disposition
    operator: str = "operator"
    note: str = ""
    actions: list[HumanAction] = field(default_factory=list)
    resumed_at: datetime | None = None


class SessionController:
    """The control lease for one live session."""

    def __init__(self) -> None:
        self._owner: Controller = "automation"
        self._epoch = 0
        self._waiter: asyncio.Future[HandoffResult] | None = None
        self._human_actions: list[HumanAction] = []
        self._ceded_at: datetime | None = None

    # ------------------------------------------------------------------ state

    @property
    def owner(self) -> Controller:
        return self._owner

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def awaiting_human(self) -> bool:
        return self._waiter is not None and not self._waiter.done()

    @property
    def human_actions(self) -> list[HumanAction]:
        return list(self._human_actions)

    # ------------------------------------------------------------------ guards

    def assert_owner(self, who: Controller = "automation", epoch: int | None = None) -> None:
        """Raise unless `who` may act right now.

        Called from the surface adapter before every action, so the check cannot be
        skipped by a caller that forgot about it.
        """
        if self._owner != who:
            raise ControlLeaseViolation(held_by=self._owner, attempted_by=who)
        if epoch is not None and epoch != self._epoch:
            raise StaleEpochError(expected=self._epoch, got=epoch)

    def guard(self, who: Controller = "automation"):  # type: ignore[no-untyped-def]
        """A zero-argument callable for injection into the surface."""

        def _check() -> None:
            self.assert_owner(who)

        return _check

    # ------------------------------------------------------------------ transfer

    async def cede(self) -> HandoffResult:
        """Hand the lease to a human and park until it comes back.

        The coroutine suspends here on a future, inside the same event loop that serves
        the operator console. Nothing is torn down; the browser stays exactly as the
        operator will find it. This is the reason the whole system uses the async
        Playwright API - the synchronous one cannot suspend and keep serving.
        """
        if self._owner == "human":
            raise ControlLeaseViolation(held_by="human", attempted_by="automation")

        self._owner = "human"
        self._epoch += 1
        self._ceded_at = datetime.now(UTC)
        self._human_actions.clear()
        self._waiter = asyncio.get_running_loop().create_future()
        return await self._waiter

    def record_human_action(self, action: HumanAction) -> None:
        """Only counted while the human actually holds the lease.

        Browser events can arrive slightly after a resume; attributing those to the
        operator would put actions in the audit trail they did not perform.
        """
        if self._owner == "human":
            self._human_actions.append(action)

    def resume(
        self,
        disposition: Disposition,
        *,
        operator: str = "operator",
        note: str = "",
    ) -> HandoffResult:
        """Take the lease back and wake the parked automation."""
        if self._owner != "human":
            raise ControlLeaseViolation(held_by=self._owner, attempted_by="human")

        result = HandoffResult(
            disposition=disposition,
            operator=operator,
            note=note,
            actions=list(self._human_actions),
            resumed_at=datetime.now(UTC),
        )

        self._owner = "automation"
        self._epoch += 1

        waiter, self._waiter = self._waiter, None
        if waiter is not None and not waiter.done():
            waiter.set_result(result)
        return result

    def abandon(self, reason: str = "run cancelled") -> None:
        """Fail the parked coroutine rather than leaving it suspended forever.

        Without this a shutdown while an intervention is open would hang the process on
        a future nobody will ever complete.
        """
        waiter, self._waiter = self._waiter, None
        self._owner = "automation"
        self._epoch += 1
        if waiter is not None and not waiter.done():
            waiter.set_exception(RuntimeError(reason))
