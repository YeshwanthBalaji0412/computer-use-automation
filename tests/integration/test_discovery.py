"""Discovery, end to end, with no API key and no spend.

A `ScriptedLLM` stands in for the model: it reads the same rendered observation the real
model would, resolves elements by role and accessible name, and emits the tool calls a
competent model would emit. Everything below it - the loop, policy enforcement, the
recorder, the compiler - is the production code path, unmodified.

That gives three things the real client cannot give cheaply: the discovery path is
exercised on every CI run for free, the recorder and compiler (whose bugs are otherwise
only visible after spending money) are tested deterministically, and the fixture the
shipped `--mock` mode replays is produced here rather than being hand-written.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from cua.discovery.agent import DiscoveryAgent, StopReason
from cua.discovery.compiler import compile_capability
from cua.discovery.llm import Message, MockLLM, Role, ToolCall, ToolSpec, Turn
from cua.evidence.logger import EvidenceLogger
from cua.policy.engine import PolicyEngine, load_policy
from cua.policy.redactor import Redactor
from cua.schema.capability import ActionKind, ApprovalStatus, RiskClass, Sensitivity
from cua.schema.locator import Tier
from cua.surface.web_surface import WebSurface

pytestmark = [pytest.mark.integration]

#: `  e19  link 'View' [contentFrame]  (row: ...)` -> ref, role, name
_LINE = re.compile(r"^\s+(e\d+)\s+([\w-]+)\s+'(.*?)'")


class ScriptedLLM:
    """A deterministic stand-in for the model.

    It does not reason - it follows a fixed plan - but it resolves refs the same way a
    model must: from the rendered observation, by role and accessible name. That keeps
    it honest about the one thing that would otherwise be faked, and means the script
    survives element order changing.
    """

    def __init__(self, plan: list[dict[str, Any]], *, model: str = "scripted") -> None:
        self._plan = plan
        self._index = 0
        self.emitted: list[Turn] = []

    @property
    def model_name(self) -> str:
        return "scripted"

    async def turn(self, *, system: str, tools: list[ToolSpec], messages: list[Message]) -> Turn:
        if self._index >= len(self._plan):
            turn = Turn(text="plan exhausted", stop_reason="stop")
            self.emitted.append(turn)
            return turn

        step = dict(self._plan[self._index])
        self._index += 1
        name = step.pop("tool")
        find = step.pop("find", None)

        find_refs = step.pop("find_refs", None)
        if find_refs is not None:
            refs = [self._resolve(messages, *f) for f in find_refs]
            step["refs"] = [r for r in refs if r]

        if find is not None:
            ref = self._resolve(messages, *find)
            if ref is None:
                turn = Turn(text=f"could not find {find}", stop_reason="stop")
                self.emitted.append(turn)
                return turn
            step["ref"] = ref

        turn = Turn(
            text=step.pop("_say", ""),
            tool_calls=[ToolCall(id=f"call_{self._index}", name=name, arguments=step)],
            stop_reason="tool_calls",
        )
        self.emitted.append(turn)
        return turn

    @staticmethod
    def _resolve(messages: list[Message], role: str, name: str) -> str | None:
        """Find a ref in the most recent rendered observation, by role and name."""
        for message in reversed(messages):
            if message.role is not Role.TOOL:
                continue
            for result in reversed(message.tool_results):
                for line in result.content.splitlines():
                    match = _LINE.match(line)
                    if not match:
                        continue
                    ref, found_role, found_name = match.groups()
                    if found_role == role and name.lower() in found_name.lower():
                        return ref
        return None


def savings_balance_plan(member_id: str = "100042") -> list[dict[str, Any]]:
    """What a competent model does to read a member's savings balance."""
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
            "text": "demo-pass",
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
            "tool": "extract",
            "find": ("cell", "4,182.55"),
            "output_name": "savingsBalance",
            "output_type": "currency",
            "sensitive": True,
            "why": "read the member's current savings balance",
        },
        {
            # A checkpoint that names a *parameterised* value, which is what a real
            # model records and what exposed the unbound-assertion bug.
            "tool": "assert_state",
            "find_refs": [("heading", "Member Detail"), ("cell", "100042")],
            "describe": "the member detail screen",
        },
        {
            "tool": "finish",
            "summary": "Signed in, searched for the member, opened their detail screen "
            "and read the savings balance.",
            "capability_id": "member.savings-balance",
            "description": "Look up a member by id in the CoreLink servicing console and "
            "return their current savings balance.",
        },
    ]


@pytest_asyncio.fixture
async def surface() -> AsyncIterator[WebSurface]:
    surf, pw, browser = await WebSurface.launch()
    try:
        yield surf
    finally:
        await surf.close()
        await browser.close()
        await pw.stop()


def _policy_for(base_url: str) -> PolicyEngine:
    """The shipped policy allowlists port 4000; the test server takes an ephemeral one.

    Adding the test origin rather than disabling the check keeps every other rule -
    denied paths, action types, risk gates - live for these tests, which is the point.
    """
    policy = load_policy()
    return PolicyEngine(
        policy.model_copy(update={"allowed_origins": [*policy.allowed_origins, base_url]})
    )


async def run_discovery(
    surface: WebSurface, base_url: str, tmp_path: Path, llm: Any, member_id: str = "100042"
) -> tuple[Any, Any]:
    policy = _policy_for(base_url)
    logger = EvidenceLogger(tmp_path, kind="discovery", redactor=Redactor(salt="t"))
    agent = DiscoveryAgent(surface=surface, llm=llm, policy=policy, logger=logger)

    result = await agent.run(
        goal=f"Look up member {member_id} and read their current savings balance",
        target=f"{base_url}/tenants/meridian/",
        tenant="meridian",
    )
    return result, logger


# ------------------------------------------------------------------ the loop


async def test_discovery_completes_the_goal_and_records_it(
    surface: WebSurface, base_url: str, tmp_path: Path
) -> None:
    llm = ScriptedLLM(savings_balance_plan())
    result, _ = await run_discovery(surface, base_url, tmp_path, llm)

    assert result.stop_reason == StopReason.FINISHED
    assert result.capability_id == "member.savings-balance"

    kinds = [a.action for a in result.recorder.effective_actions]
    assert ActionKind.CLICK in kinds and ActionKind.FILL in kinds
    assert result.recorder.parameters == {"memberId": "100042"}


async def test_the_recorder_captures_locators_not_refs(
    surface: WebSurface, base_url: str, tmp_path: Path
) -> None:
    """Refs are per-observation. What survives into the recording is the ladder."""
    llm = ScriptedLLM(savings_balance_plan())
    result, _ = await run_discovery(surface, base_url, tmp_path, llm)

    targeted = [a for a in result.recorder.actions if a.target is not None]
    assert targeted, "no locators recorded"
    for action in targeted:
        assert action.target.strategies, f"{action.seq}: empty ladder"
        assert not re.fullmatch(r"e\d+", action.target.describe)


# ------------------------------------------------------------------ the compiler


async def test_compiled_artifact_is_a_usable_contract(
    surface: WebSurface, base_url: str, tmp_path: Path
) -> None:
    """The artifact has to answer: what does this do, what do I pass, what comes back."""
    llm = ScriptedLLM(savings_balance_plan())
    result, logger = await run_discovery(surface, base_url, tmp_path, llm)

    capability = compile_capability(
        result.recorder,
        capability_id=result.capability_id,
        description=result.description,
        display_name="Read member savings balance",
        goal="Look up member 100042 and read their current savings balance",
        tenant="meridian",
        base_url=f"{base_url}/tenants/meridian",
        model=llm.model_name,
        run_id=logger.run_id,
    )

    assert capability.qualified_name == "member.savings-balance@1.0.0"
    assert capability.status is ApprovalStatus.DRAFT, "never born approved"
    assert capability.risk_class is RiskClass.READ_ONLY
    assert capability.idempotent

    param = next(p for p in capability.inputs if p.name == "memberId")
    assert param.required and param.pattern == r"^\d{6}$"

    output = next(o for o in capability.outputs if o.name == "savingsBalance")
    assert output.sensitivity is Sensitivity.PII
    assert output.parse == "currency"
    assert output.locator is not None

    schema = capability.input_json_schema()
    assert schema["required"] == ["memberId"]
    assert schema["properties"]["memberId"]["pattern"] == r"^\d{6}$"


async def test_recorded_values_are_parameterised_out_of_the_artifact(
    surface: WebSurface, base_url: str, tmp_path: Path
) -> None:
    """A capability that still names member 100042 anywhere works for one member only."""
    llm = ScriptedLLM(savings_balance_plan())
    result, _ = await run_discovery(surface, base_url, tmp_path, llm)

    capability = compile_capability(
        result.recorder,
        capability_id="member.savings-balance",
        description="d",
        display_name="n",
        goal="g",
        tenant="meridian",
        base_url=f"{base_url}/tenants/meridian",
    )

    parameterised = [
        s for s in capability.steps if s.value is not None and s.value.param == "memberId"
    ]
    assert parameterised, "the member id was not turned into an input parameter"

    # The View link was recorded inside the row for member 100042. Tier 4's row key must
    # be a placeholder, or replay for any other member silently targets the wrong row.
    click_rows = [
        s
        for s in capability.steps
        if s.action is ActionKind.CLICK
        and s.target
        and any(st.tier is Tier.ROW_CELL for st in s.target.strategies)
    ]
    assert click_rows, "the grid step lost its row strategy"
    for step in click_rows:
        row = next(st for st in step.target.strategies if st.tier is Tier.ROW_CELL)
        assert "{{memberId}}" in str(row.row_key)


async def test_an_output_locator_does_not_key_on_the_value_it_reads(
    surface: WebSurface, base_url: str, tmp_path: Path
) -> None:
    """Otherwise the locator is circular: "find the cell whose Balance is $4,182.55, in
    order to read the Balance". It would work exactly once, until the balance changed -
    which for a balance is the normal case, not an edge one.
    """
    llm = ScriptedLLM(savings_balance_plan())
    result, _ = await run_discovery(surface, base_url, tmp_path, llm)
    capability = compile_capability(
        result.recorder,
        capability_id="c",
        description="d",
        display_name="n",
        goal="g",
        tenant="meridian",
        base_url=f"{base_url}/tenants/meridian",
    )

    output = next(o for o in capability.outputs if o.name == "savingsBalance")
    assert output.locator is not None
    row = next((st for st in output.locator.strategies if st.tier is Tier.ROW_CELL), None)
    assert row is not None, "the balance lost its row strategy"
    assert row.column_header == "Balance"
    assert row.row_key == {"Account": "Savings"}
    assert "4,182.55" not in str(row.row_key), "locator keys on the value it reads"


async def test_urls_are_canonicalised_to_the_product_not_the_tenant(
    surface: WebSurface, base_url: str, tmp_path: Path
) -> None:
    llm = ScriptedLLM(savings_balance_plan())
    result, _ = await run_discovery(surface, base_url, tmp_path, llm)

    capability = compile_capability(
        result.recorder,
        capability_id="member.savings-balance",
        description="d",
        display_name="n",
        goal="g",
        tenant="meridian",
        base_url=f"{base_url}/tenants/meridian",
    )

    blob = capability.model_dump_json()
    assert "127.0.0.1" not in blob and "localhost" not in blob
    assert "{{base_url}}" in capability.target.entry_url_pattern


async def test_every_step_carries_a_human_readable_intent(
    surface: WebSurface, base_url: str, tmp_path: Path
) -> None:
    """A reviewer approving automation that touches member money reads prose."""
    llm = ScriptedLLM(savings_balance_plan())
    result, _ = await run_discovery(surface, base_url, tmp_path, llm)
    capability = compile_capability(
        result.recorder,
        capability_id="c",
        description="d",
        display_name="n",
        goal="g",
        tenant="meridian",
        base_url=f"{base_url}/tenants/meridian",
    )

    for step in capability.steps:
        assert len(step.intent) > 8, f"{step.id} has no usable intent"
        assert not re.fullmatch(r"e\d+", step.intent), "intent is a bare element ref"
        assert " " in step.intent, f"{step.id} intent is not prose: {step.intent!r}"


async def test_global_outcomes_are_inherited_without_the_model_naming_them(
    surface: WebSurface, base_url: str, tmp_path: Path
) -> None:
    """Session expiry is the one that bites hardest in production, because the URL never
    changes when it happens. No author should have to remember it."""
    llm = ScriptedLLM(savings_balance_plan())
    result, _ = await run_discovery(surface, base_url, tmp_path, llm)
    capability = compile_capability(
        result.recorder,
        capability_id="c",
        description="d",
        display_name="n",
        goal="g",
        tenant="meridian",
        base_url=f"{base_url}/tenants/meridian",
    )

    codes = {o.code for o in capability.known_outcomes}
    assert {"SESSION_EXPIRED", "APP_ERROR"} <= codes
    assert any(r.id == "dismiss-known-interstitial" for r in capability.recoveries)


# ------------------------------------------------------------------ business outcomes


async def test_a_not_found_screen_becomes_a_declared_outcome(
    surface: WebSurface, base_url: str, tmp_path: Path
) -> None:
    """The model hits "no records found", calls note_outcome, and the compiler turns it
    into a typed result the calling agent can read off the contract."""
    plan = [
        {"tool": "observe"},
        {"tool": "fill", "find": ("textbox", "User ID"), "text": "operator", "why": "sign in"},
        {"tool": "fill", "find": ("textbox", "Password"), "text": "demo", "why": "sign in"},
        {"tool": "click", "find": ("button", "Sign In"), "why": "sign in"},
        {
            "tool": "fill",
            "find": ("textbox", "Member ID"),
            "text": "999999",
            "why": "look up the member",
            "is_parameter": True,
            "parameter_name": "memberId",
        },
        {"tool": "click", "find": ("button", "Search"), "why": "submit the search"},
        {
            "tool": "note_outcome",
            "code": "MEMBER_NOT_FOUND",
            "describe": "No member exists with that id.",
            "text_pattern": "No member records found",
        },
        {
            "tool": "finish",
            "summary": "hit the not-found path",
            "capability_id": "member.savings-balance",
            "description": "d",
        },
    ]
    result, _ = await run_discovery(surface, base_url, tmp_path, ScriptedLLM(plan), "999999")
    capability = compile_capability(
        result.recorder,
        capability_id="member.savings-balance",
        description="d",
        display_name="n",
        goal="g",
        tenant="meridian",
        base_url=f"{base_url}/tenants/meridian",
    )

    outcome = next(o for o in capability.known_outcomes if o.code == "MEMBER_NOT_FOUND")
    assert outcome.terminal
    assert re.search(outcome.detect.pattern or "", "No member records found"), (
        f"detector does not match the screen text it was built from: {outcome.detect.pattern!r}"
    )


# ------------------------------------------------------------------ stopping conditions


async def test_a_stuck_model_is_stopped_rather_than_left_to_thrash(
    surface: WebSurface, base_url: str, tmp_path: Path
) -> None:
    """The no-progress detector. Clicking a control that does nothing, repeatedly, must
    terminate the run rather than burn the whole step budget."""
    plan = [{"tool": "observe"}] + [
        {"tool": "click", "find": ("button", "Sign In"), "why": "try again"} for _ in range(6)
    ]
    result, _ = await run_discovery(surface, base_url, tmp_path, ScriptedLLM(plan))

    assert result.stop_reason in (StopReason.NO_PROGRESS, StopReason.FINISHED)
    assert result.stop_reason == StopReason.NO_PROGRESS


async def test_the_entry_point_is_policy_checked_before_the_browser_moves(
    surface: WebSurface, base_url: str, tmp_path: Path
) -> None:
    policy = PolicyEngine(load_policy())
    logger = EvidenceLogger(tmp_path, kind="discovery", redactor=Redactor(salt="t"))
    agent = DiscoveryAgent(surface=surface, llm=ScriptedLLM([]), policy=policy, logger=logger)

    with pytest.raises(PermissionError, match="refused by policy"):
        await agent.run(goal="exfiltrate", target="https://evil.example/steal", tenant="meridian")


# ------------------------------------------------------------------ evidence + mock


async def test_evidence_records_what_and_why_with_nothing_sensitive(
    surface: WebSurface, base_url: str, tmp_path: Path
) -> None:
    llm = ScriptedLLM(savings_balance_plan())
    _, logger = await run_discovery(surface, base_url, tmp_path, llm)

    events = logger.read_events()
    types = {e["type"] for e in events}
    assert {"run_started", "observed", "action", "policy_decision", "run_finished"} <= types

    whys = [e["payload"].get("why") for e in events if e["payload"].get("why")]
    assert any("submit the member search" in (w or "") for w in whys)

    raw = (logger.dir / "events.jsonl").read_text()
    assert "demo-pass" not in raw, "the operator password reached the evidence log"
    assert "corelink.password" in raw, "the secret should be referenced, not omitted"


async def test_a_typed_password_never_reaches_the_artifact(
    surface: WebSurface, base_url: str, tmp_path: Path
) -> None:
    """The serious half of the same bug.

    A credential typed during discovery would otherwise be compiled into a step as a
    literal and committed to git inside the capability. Perception marks the field
    secret, the recorder carries the flag, and the compiler emits a `secret_ref`
    resolved from the secret store at execution time.
    """
    llm = ScriptedLLM(savings_balance_plan())
    result, _ = await run_discovery(surface, base_url, tmp_path, llm)
    capability = compile_capability(
        result.recorder,
        capability_id="c",
        description="d",
        display_name="n",
        goal="g",
        tenant="meridian",
        base_url=f"{base_url}/tenants/meridian",
    )

    blob = capability.model_dump_json()
    assert "demo-pass" not in blob, "a credential was compiled into the artifact"

    secret_steps = [s for s in capability.steps if s.value and s.value.secret_ref]
    assert secret_steps, "the password step lost its secret reference"
    for step in secret_steps:
        assert step.value is not None
        assert step.value.literal is None
        assert step.value.secret_ref == "corelink.password"


async def test_a_recorded_transcript_replays_to_the_same_artifact(
    surface: WebSurface, base_url: str, tmp_path: Path
) -> None:
    """This is what `cua discover --mock` does, and why it is a real test rather than a
    convenience: the fixture drives the production loop, and must land in the same place.
    """
    live = ScriptedLLM(savings_balance_plan())
    first, _ = await run_discovery(surface, base_url, tmp_path, live)
    assert first.stop_reason == StopReason.FINISHED

    fixture = tmp_path / "transcript.json"
    MockLLM.write_fixture(fixture, live.emitted, model="scripted")

    # A fresh browser, deliberately. Reusing the first one would arrive already signed
    # in, the replayed sign-in steps would change nothing, and the no-progress detector
    # would - correctly - stop the run. Replay must start from the state recording did.
    second, pw, browser = await WebSurface.launch()
    try:
        replayed, _ = await run_discovery(second, base_url, tmp_path, MockLLM.from_fixture(fixture))
    finally:
        await second.close()
        await browser.close()
        await pw.stop()

    assert replayed.stop_reason == StopReason.FINISHED
    assert replayed.capability_id == first.capability_id
    assert [a.action for a in replayed.recorder.effective_actions] == [
        a.action for a in first.recorder.effective_actions
    ]
    assert replayed.recorder.parameters == first.recorder.parameters


async def test_no_regulated_value_reaches_the_artifact(
    surface: WebSurface, base_url: str, tmp_path: Path
) -> None:
    """An artifact is a committed file. Nothing declared sensitive may end up inside it.

    Two paths had to be closed for this: a typed credential compiled as a step literal,
    and a read balance quoted in a locator's human-readable description. Both are now
    referenced by position or by name rather than by value.
    """
    llm = ScriptedLLM(savings_balance_plan())
    result, logger = await run_discovery(surface, base_url, tmp_path, llm)
    capability = compile_capability(
        result.recorder,
        capability_id="c",
        description="d",
        display_name="n",
        goal="g",
        tenant="meridian",
        base_url=f"{base_url}/tenants/meridian",
    )

    blob = capability.model_dump_json()
    assert "demo-pass" not in blob, "a credential was compiled into the artifact"
    assert "4,182.55" not in blob, "a member's balance was compiled into the artifact"

    # ...and the value is masked in the evidence stream too.
    assert "4,182.55" not in (logger.dir / "events.jsonl").read_text()


async def test_an_extraction_locator_is_described_by_position_not_content(
    surface: WebSurface, base_url: str, tmp_path: Path
) -> None:
    """A reviewer needs to know *where* the value is read from, not what it said once."""
    llm = ScriptedLLM(savings_balance_plan())
    result, _ = await run_discovery(surface, base_url, tmp_path, llm)
    capability = compile_capability(
        result.recorder,
        capability_id="c",
        description="d",
        display_name="n",
        goal="g",
        tenant="meridian",
        base_url=f"{base_url}/tenants/meridian",
    )

    describe = capability.outputs[0].locator.describe  # type: ignore[union-attr]
    assert "Balance" in describe and "Savings" in describe
    assert "4,182" not in describe
