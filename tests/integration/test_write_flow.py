"""Flow B: recording an irreversible write, which discovery refuses to do unsupervised.

The read-only flow proves the loop works. This one proves the *safety* model works, and
it is the harder half of the brief: the moment an agent's action cannot be undone, "the
model figured it out" stops being a sufficient answer.

Policy escalates every write during discovery - the model is by definition operating on a
UI it does not yet understand, which is the worst possible moment to let it press "Confirm
and Open Account". So a write flow can only be recorded *attended*: a human authorises
each risky action through the same lease, the same intervention store and the same console
that replay hands off to. Replay escalates because it is stuck; discovery escalates because
it is about to do something irreversible. One mechanism, two rules.

Four paths are covered here, and the last two matter as much as the first:

    authorised    the operator says yes, automation performs its own proposed action,
                  and the step is recorded with the model's intent intact
    refused       the operator says no, the run ends, and nothing was opened
    unattended    nobody is listening, so the answer is no - a recording session must not
                  be able to open an account merely because no one was watching
    idempotency   the compiled artifact is marked non-idempotent, because replaying it
                  blindly would open a second account
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cua.control.intervention import InterventionStore
from cua.control.session import Disposition, SessionController
from cua.discovery.agent import DiscoveryAgent, StopReason
from cua.discovery.compiler import compile_capability
from cua.evidence.logger import EvidenceLogger
from cua.policy.redactor import Redactor
from cua.schema.capability import ActionKind, ApprovalStatus, RiskClass
from cua.surface.web_surface import WebSurface
from tests.integration.test_discovery import ScriptedLLM, _policy_for, surface  # noqa: F401

pytestmark = [pytest.mark.integration]

NICKNAME = "HOLIDAY FUND"

#: The target app keeps opened sub-accounts in module state and refuses a duplicate, which
#: is exactly what a core banking system should do. Each test that actually opens an
#: account therefore needs its own nickname, or the second one to run gets a legitimate
#: DUPLICATE_RECORD instead of the confirm screen. That is the app being correct, not a
#: test fixture being awkward - and it is the same property the capability relies on to be
#: safe to attempt twice.


def open_subaccount_plan(
    member_id: str = "100042", nickname: str = NICKNAME
) -> list[dict[str, Any]]:
    """What a competent model does to open a sub-account.

    Note there is nothing defensive in here. The plan walks straight at the confirm
    button; every guard that stops it is the system's, not the script's. A plan that
    politely avoided the risky step would test nothing.
    """
    return [
        {"tool": "observe", "_say": "Looking at the sign-in screen."},
        {
            "tool": "fill",
            "find": ("textbox", "User ID"),
            "text": "operator",
            "why": "sign in as the servicing operator",
        },
        {
            "tool": "fill",
            "find": ("textbox", "Password"),
            "text": "<secret:corelink.password>",
            "why": "supply the operator password",
        },
        {"tool": "click", "find": ("button", "Sign In"), "why": "sign in to the console"},
        {
            "tool": "fill",
            "find": ("textbox", "Member ID"),
            "text": member_id,
            "why": "enter the member number to look up",
            "is_parameter": True,
            "parameter_name": "memberId",
        },
        {"tool": "click", "find": ("button", "Search"), "why": "submit the member search"},
        {
            "tool": "click",
            "find": ("link", "View"),
            "why": "open the matching member's detail screen",
        },
        {
            "tool": "click",
            "find": ("link", "Open Sub-Account"),
            "why": "start the new sub-account form for this member",
        },
        {
            "tool": "fill",
            "find": ("textbox", "Account Nickname"),
            "text": nickname,
            "why": "name the new sub-account",
            "is_parameter": True,
            "parameter_name": "nickname",
        },
        {
            "tool": "select",
            "find": ("combobox", "Account Type"),
            "value": "SAVINGS",
            "why": "open it as a savings sub-account",
            "is_parameter": True,
            "parameter_name": "accountType",
        },
        # Escalation 1. "Continue" only reaches a review screen, but the classifier reads
        # the words on the button and cannot know that. Being asked twice during a
        # one-time supervised recording is cheap; being asked zero times when it counted
        # is not.
        {"tool": "click", "find": ("button", "Continue"), "why": "review the new sub-account"},
        # Escalation 2. The real one.
        {
            "tool": "click",
            "find": ("button", "Confirm and Open Account"),
            "why": "commit the new sub-account",
        },
        {
            "tool": "extract",
            "find": ("cell", "SA-"),
            "output_name": "confirmationNumber",
            "output_type": "text",
            "why": "capture the confirmation number the caller needs as a receipt",
        },
        {
            "tool": "assert_state",
            "find_refs": [("heading", "Sub-Account Opened")],
            "describe": "the sub-account confirmation screen",
        },
        {
            "tool": "finish",
            "summary": "Signed in, found the member, opened the sub-account form, and "
            "confirmed the new account.",
            "capability_id": "member.open-subaccount",
            "description": "Open a new sub-account for a member in the CoreLink servicing "
            "console and return the confirmation number.",
        },
    ]


async def authorise_when_asked(
    controller: SessionController,
    disposition: Disposition,
    *,
    stop: asyncio.Event,
    limit: int = 4,
) -> int:
    """Stand in for the operator at the console.

    Drives `SessionController` directly rather than the HTTP API - the console's own
    endpoints are covered in `test_handoff.py`, and what is under test here is the
    discovery agent's half of the transfer.
    """
    granted = 0
    while not stop.is_set() and granted < limit:
        if controller.awaiting_human:
            controller.resume(disposition, operator="reviewer", note="authorised for recording")
            granted += 1
        await asyncio.sleep(0.05)
    return granted


async def run_write_discovery(
    surface: WebSurface,  # noqa: F811
    base_url: str,
    tmp_path: Path,
    *,
    attended: bool,
    disposition: Disposition = Disposition.STEP_COMPLETED,
    nickname: str = NICKNAME,
    plan: list[dict[str, Any]] | None = None,
) -> tuple[Any, Any, int]:
    logger = EvidenceLogger(tmp_path, kind="discovery", redactor=Redactor(salt="t"))
    controller = SessionController() if attended else None
    store = InterventionStore(logger.dir, redactor=Redactor(salt="t")) if attended else None

    agent = DiscoveryAgent(
        surface=surface,
        llm=ScriptedLLM(plan or open_subaccount_plan(nickname=nickname)),
        policy=_policy_for(base_url),
        logger=logger,
        secrets={"corelink.password": "demo-pass"},
        controller=controller,
        interventions=store,
        operator_base_url="http://localhost:0" if attended else "",
    )

    stop = asyncio.Event()
    operator: asyncio.Task[int] | None = None
    if controller is not None:
        operator = asyncio.create_task(authorise_when_asked(controller, disposition, stop=stop))

    try:
        result = await agent.run(
            goal="Open a SAVINGS sub-account nicknamed HOLIDAY FUND for member 100042",
            target=f"{base_url}/tenants/meridian/",
            tenant="meridian",
        )
    finally:
        stop.set()
        if operator is not None:
            granted = await operator
        else:
            granted = 0
        if controller is not None:
            controller.abandon("test finished")

    return result, logger, granted


# ------------------------------------------------------------------ the safety property


async def test_unattended_discovery_will_not_perform_an_irreversible_action(
    surface: WebSurface,  # noqa: F811
    base_url: str,
    tmp_path: Path,
) -> None:
    """The property that matters most in this file.

    With nobody to ask, the answer is no. Not "log a warning and continue", and not
    "allow it because the model seemed confident" - the run ends, and the account is not
    opened. An unattended recording session must not be able to move money merely because
    no operator happened to be watching.
    """
    result, _, _ = await run_write_discovery(surface, base_url, tmp_path, attended=False)

    assert result.stop_reason == StopReason.POLICY_ESCALATION
    assert not result.succeeded

    # And crucially: it stopped *before* acting. No write action was ever recorded.
    names = [a.control_name for a in result.recorder.effective_actions]
    assert "Confirm and Open Account" not in names
    assert "Continue" not in names


async def test_a_refusal_ends_the_run_and_opens_nothing(
    surface: WebSurface,  # noqa: F811
    base_url: str,
    tmp_path: Path,
) -> None:
    """The operator's 'no' has to be as load-bearing as their 'yes'."""
    result, _, granted = await run_write_discovery(
        surface, base_url, tmp_path, attended=True, disposition=Disposition.ABORT
    )

    assert granted >= 1, "the operator was never asked"
    assert result.stop_reason == StopReason.POLICY_ESCALATION
    names = [a.control_name for a in result.recorder.effective_actions]
    assert "Confirm and Open Account" not in names


# ------------------------------------------------------------------ the recording


async def test_an_authorised_write_flow_records_end_to_end(
    surface: WebSurface,  # noqa: F811
    base_url: str,
    tmp_path: Path,
) -> None:
    """The capability the read-only flow cannot demonstrate."""
    result, _, granted = await run_write_discovery(
        surface, base_url, tmp_path, attended=True, nickname="ROOF FUND"
    )

    assert result.stop_reason == StopReason.FINISHED, result.stop_reason
    assert result.capability_id == "member.open-subaccount"
    assert granted == 2, "expected an authorisation for Continue and for the confirm button"

    # Automation performed the risky action itself, so the step carries the model's own
    # stated intent. Had the operator clicked it, there would be a role and a name and no
    # `why` - which is the difference between a step that replays and a note that
    # something once happened.
    confirm = next(
        a for a in result.recorder.effective_actions if a.control_name == "Confirm and Open Account"
    )
    assert confirm.action is ActionKind.CLICK
    assert confirm.why == "commit the new sub-account"


async def test_the_authorisation_is_recorded_as_evidence(
    surface: WebSurface,  # noqa: F811
    base_url: str,
    tmp_path: Path,
) -> None:
    """Who authorised what, on disk, without anyone having to remember to write it down."""
    _, logger, _ = await run_write_discovery(
        surface, base_url, tmp_path, attended=True, nickname="BOAT FUND"
    )

    records = sorted((logger.dir / "interventions").glob("*.json"))
    assert len(records) == 2, [p.name for p in records]

    import json

    payload = json.loads(records[-1].read_text(encoding="utf-8"))
    assert payload["reason"] == "risky_step_needs_approval"
    assert payload["disposition"] == "step_completed"
    assert payload["operator"] == "reviewer"
    assert "Confirm and Open Account" in payload["explain"]
    # The operator is told what the model was doing, in the model's words.
    assert payload["attempted"], "no context was given to the operator"

    events = (logger.dir / "events.jsonl").read_text(encoding="utf-8")
    assert "control_transferred" in events
    assert "allow_after_authorisation" in events


async def test_the_compiled_capability_is_marked_dangerous(
    surface: WebSurface,  # noqa: F811
    base_url: str,
    tmp_path: Path,
) -> None:
    """The artifact has to carry the risk forward, or the whole gate is one-time theatre.

    Two independent facts end up in the file: the flow is irreversible, and it has not
    been reviewed. Replay refuses on either one.
    """
    result, logger, _ = await run_write_discovery(
        surface, base_url, tmp_path, attended=True, nickname="TUITION FUND"
    )

    capability = compile_capability(
        result.recorder,
        capability_id=result.capability_id,
        description=result.description,
        display_name="Open a member sub-account",
        goal="Open a SAVINGS sub-account for a member",
        tenant="meridian",
        base_url=f"{base_url}/tenants/meridian",
        model="scripted",
        prompt_version="test",
        run_id=logger.run_id,
    )

    assert capability.risk_class is RiskClass.IRREVERSIBLE_WRITE
    assert capability.status is ApprovalStatus.DRAFT
    # Replaying this blindly would open a second account. The flag is what tells a
    # calling agent - and any retry logic - that this is not safe to repeat.
    assert capability.idempotent is False

    # The parameters a caller must supply were inferred from the recording.
    assert {p.name for p in capability.inputs} >= {"memberId", "nickname"}
    assert [o.name for o in capability.outputs] == ["confirmationNumber"]

    # The credential is a reference, never a literal - same discipline as the read flow.
    dumped = capability.model_dump_json()
    assert "demo-pass" not in dumped
