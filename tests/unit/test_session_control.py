"""The control lease.

The brief wants "a way to know who is (or should be) in control". These tests pin down
what makes this a lease rather than a flag: exclusivity that *raises*, an epoch that
invalidates work already in flight, and a park that can be woken or abandoned but never
silently dropped.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from cua.control.intervention import (
    Intervention,
    InterventionStatus,
    InterventionStore,
    suggest,
)
from cua.control.session import (
    ControlLeaseViolation,
    Disposition,
    HumanAction,
    SessionController,
    StaleEpochError,
)
from cua.schema.result import EscalationReason

pytestmark = pytest.mark.unit


def action(kind: str = "click", name: str = "Close") -> HumanAction:
    return HumanAction(at=datetime.now(UTC), kind=kind, role="button", name=name)


# ------------------------------------------------------------------ exclusivity


def test_automation_holds_the_lease_by_default() -> None:
    controller = SessionController()
    assert controller.owner == "automation"
    controller.assert_owner("automation")


async def test_automation_cannot_act_while_a_human_holds_the_lease() -> None:
    """Not "politely waits" - raises. The guard is injected into the surface adapter, so
    the prohibition sits below anything that might forget to check it."""
    controller = SessionController()
    parked = asyncio.create_task(controller.cede())
    await asyncio.sleep(0)

    assert controller.owner == "human"
    with pytest.raises(ControlLeaseViolation) as exc:
        controller.assert_owner("automation")
    assert exc.value.held_by == "human"

    controller.resume(Disposition.RECOVERED)
    await parked


async def test_the_injected_guard_enforces_the_same_rule() -> None:
    controller = SessionController()
    guard = controller.guard()
    guard()  # automation holds it; no raise

    parked = asyncio.create_task(controller.cede())
    await asyncio.sleep(0)
    with pytest.raises(ControlLeaseViolation):
        guard()

    controller.resume(Disposition.ABORT)
    await parked


# ------------------------------------------------------------------ the epoch


async def test_the_epoch_invalidates_work_that_was_already_in_flight() -> None:
    """The real race in this design.

    An automation action can be mid-await when a human takes over. Without a monotonic
    epoch that action lands *after* the transfer, on a page the operator is editing.
    A boolean cannot express this; the epoch turns silent corruption into a loud error.
    """
    controller = SessionController()
    epoch_at_dispatch = controller.epoch

    parked = asyncio.create_task(controller.cede())
    await asyncio.sleep(0)
    controller.resume(Disposition.RECOVERED)
    await parked

    # Automation holds the lease again, so the owner check alone would pass...
    controller.assert_owner("automation")
    # ...but the action was dispatched two transfers ago.
    with pytest.raises(StaleEpochError):
        controller.assert_owner("automation", epoch=epoch_at_dispatch)


async def test_every_transfer_advances_the_epoch() -> None:
    controller = SessionController()
    start = controller.epoch

    parked = asyncio.create_task(controller.cede())
    await asyncio.sleep(0)
    assert controller.epoch == start + 1

    controller.resume(Disposition.RECOVERED)
    await parked
    assert controller.epoch == start + 2


# ------------------------------------------------------------------ park and resume


async def test_ceding_parks_until_a_human_resumes() -> None:
    """The park is a suspended coroutine, not a spin or a sleep - which is what lets the
    same event loop keep serving the operator console while the run waits."""
    controller = SessionController()
    parked = asyncio.create_task(controller.cede())
    await asyncio.sleep(0)

    assert controller.awaiting_human
    assert not parked.done()

    controller.resume(Disposition.STEP_COMPLETED, operator="alex", note="did it by hand")
    handoff = await parked

    assert handoff.disposition is Disposition.STEP_COMPLETED
    assert handoff.operator == "alex"
    assert handoff.note == "did it by hand"
    assert controller.owner == "automation"


async def test_the_operators_actions_travel_with_the_handoff() -> None:
    controller = SessionController()
    parked = asyncio.create_task(controller.cede())
    await asyncio.sleep(0)

    controller.record_human_action(action("click", "Acknowledge"))
    controller.record_human_action(action("submit", "Search"))
    controller.resume(Disposition.RECOVERED)

    handoff = await parked
    assert [a.name for a in handoff.actions] == ["Acknowledge", "Search"]
    assert "Acknowledge" in handoff.actions[0].describe()


async def test_actions_are_only_attributed_to_a_human_who_holds_the_lease() -> None:
    """Browser events can arrive just after a resume. Attributing those to the operator
    would put actions in the audit trail they did not perform."""
    controller = SessionController()
    controller.record_human_action(action("click", "Before"))
    assert controller.human_actions == []

    parked = asyncio.create_task(controller.cede())
    await asyncio.sleep(0)
    controller.record_human_action(action("click", "During"))
    controller.resume(Disposition.RECOVERED)
    await parked

    controller.record_human_action(action("click", "After"))
    assert [a.name for a in controller.human_actions] == ["During"]


async def test_abandoning_fails_the_parked_coroutine_rather_than_hanging() -> None:
    """A shutdown while an intervention is open must not leave a coroutine suspended on
    a future nobody will ever complete."""
    controller = SessionController()
    parked = asyncio.create_task(controller.cede())
    await asyncio.sleep(0)

    controller.abandon("run cancelled")
    with pytest.raises(RuntimeError, match="run cancelled"):
        await parked
    assert controller.owner == "automation"


async def test_resuming_when_nobody_holds_the_lease_is_refused() -> None:
    controller = SessionController()
    with pytest.raises(ControlLeaseViolation):
        controller.resume(Disposition.RECOVERED)


async def test_ceding_twice_is_refused() -> None:
    controller = SessionController()
    parked = asyncio.create_task(controller.cede())
    await asyncio.sleep(0)

    with pytest.raises(ControlLeaseViolation):
        await controller.cede()

    controller.resume(Disposition.ABORT)
    await parked


# ------------------------------------------------------------------ interventions


def make(reason: EscalationReason = EscalationReason.UNKNOWN_DIALOG) -> Intervention:
    return Intervention(
        id="int_1",
        run_id="rep_1",
        capability="member.savings-balance@1.0.0",
        goal="read a savings balance",
        step_id="s6",
        step_intent="open the matching member's detail screen",
        reason=reason,
        explain="a dialog titled 'Regulation CC Hold Notice' is not declared",
        suggested_actions=suggest(reason),
    )


def test_an_intervention_carries_what_an_operator_needs_to_act() -> None:
    """The brief's list: which capability, the current step, the state, and why it
    stopped - plus what was already tried, so nobody repeats it."""
    item = make()
    assert item.capability and item.goal
    assert item.step_id and item.step_intent
    assert item.reason and item.explain
    assert item.suggested_actions
    assert any("abort" in s.lower() for s in item.suggested_actions)


def test_the_store_tracks_the_lifecycle(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = InterventionStore(tmp_path)
    store.raise_(make())

    assert [i.id for i in store.open_items()] == ["int_1"]
    assert (tmp_path / "interventions" / "int_1.json").exists()

    assert store.take("int_1", operator="alex") is not None
    assert store.get("int_1").status is InterventionStatus.TAKEN  # type: ignore[union-attr]
    assert store.take("int_1", operator="sam") is None, "already taken"

    resolved = store.resolve(
        "int_1", Disposition.RECOVERED, note="dismissed it", actions=[action()]
    )
    assert resolved is not None
    assert resolved.status is InterventionStatus.RESOLVED
    assert resolved.human_actions == ["click button 'Close'"]
    assert store.open_items() == []


def test_an_ambiguous_write_tells_the_operator_to_check_before_retrying() -> None:
    """The most dangerous escalation there is: the write may or may not have landed, so
    the advice has to lead with 'check', not 'retry'."""
    advice = suggest(EscalationReason.AMBIGUOUS_WRITE_OUTCOME)
    assert "check whether the write landed" in advice[0].lower()
    assert "twice" in advice[0].lower()
