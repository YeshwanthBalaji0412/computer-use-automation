"""Run evidence.

The property that matters most is negative: **nothing reaches disk unredacted**. There is
one writer, and it redacts. These tests are what make that a checkable claim rather than
a habit that erodes the first time someone adds a debug log.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cua.evidence.logger import EventType, EvidenceLogger
from cua.policy.redactor import Redactor

pytestmark = pytest.mark.unit


@pytest.fixture
def logger(tmp_path: Path) -> EvidenceLogger:
    redactor = Redactor(salt="test")
    redactor.register("memberId", "100042")
    return EvidenceLogger(tmp_path, kind="replay", redactor=redactor)


def test_events_are_jsonl_in_order(logger: EvidenceLogger) -> None:
    logger.event(EventType.RUN_STARTED, capability="member.balance@1.0.0")
    logger.event(EventType.STEP_STARTED, step_id="s1", intent="open member search")
    logger.event(EventType.STEP_FINISHED, step_id="s1", status="ok")

    events = logger.read_events()
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert [e["type"] for e in events] == ["run_started", "step_started", "step_finished"]
    assert events[1]["step_id"] == "s1"


def test_every_event_records_who_acted(logger: EvidenceLogger) -> None:
    """The handoff audit trail falls out of the same stream rather than needing its own:
    automation and a human operator write to one log, distinguished by `actor`."""
    logger.event(EventType.ACTION, actor="automation", why="submit the search")
    logger.event(EventType.HUMAN_ACTION, actor="human", why="dismissed a hold notice")

    actors = [e["actor"] for e in logger.read_events()]
    assert actors == ["automation", "human"]


def test_nothing_reaches_disk_unredacted(logger: EvidenceLogger) -> None:
    """The single most important property of this module."""
    logger.event(
        EventType.OBSERVED,
        text="member 100042, ssn 123-45-6789, j.rivera@example.com",
        nested={"detail": ["100042", "safe value"]},
    )

    raw = (logger.dir / "events.jsonl").read_text()
    assert "100042" not in raw
    assert "123-45-6789" not in raw
    assert "j.rivera@example.com" not in raw
    assert "«redacted:memberId»" in raw
    assert "safe value" in raw, "non-sensitive content must survive or the log is useless"


def test_the_manifest_is_redacted_too(logger: EvidenceLogger) -> None:
    logger.write_manifest(
        capability="member.balance@1.0.0",
        inputs={"memberId": "100042"},
        result="success",
    )
    manifest = json.loads((logger.dir / "manifest.json").read_text())

    assert manifest["kind"] == "replay"
    assert manifest["inputs"]["memberId"] == "«redacted:memberId»"
    assert manifest["result"] == "success"


def test_failure_context_is_the_richer_signal(logger: EvidenceLogger) -> None:
    """A screenshot shows what it looked like; the aria snapshot shows what the system
    *perceived*. The difference between those two is usually the bug."""
    logger.write_failure_context(
        aria_snapshot='- heading "Session Ended"\n- textbox "User ID"',
        observations=[{"url": "http://x/", "member": "100042"}],
        summary="step s4 expected Member Detail, observed Session Ended",
    )

    aria = (logger.failure_dir / "aria.txt").read_text()
    obs = (logger.failure_dir / "observations.json").read_text()
    summary = (logger.failure_dir / "summary.txt").read_text()

    assert "Session Ended" in aria
    assert "100042" not in obs
    assert "expected Member Detail, observed Session Ended" in summary


def test_report_renders_the_why_not_just_the_what(logger: EvidenceLogger) -> None:
    """ "A structured log of what the agent did *and why*" - the why comes from the
    required `why` argument on every discovery tool call, which also becomes the step's
    intent in the artifact."""
    logger.event(EventType.ACTION, step_id="s3", why="submit the member search")
    report = logger.write_report("Replay run").read_text()

    assert "submit the member search" in report
    assert "| s3 |" in report


def test_runs_are_isolated_from_each_other(tmp_path: Path) -> None:
    a = EvidenceLogger(tmp_path, kind="discovery", redactor=Redactor(salt="a"))
    b = EvidenceLogger(tmp_path, kind="replay", redactor=Redactor(salt="b"))

    assert a.run_id != b.run_id
    assert a.dir != b.dir
    assert a.run_id.startswith("dis") and b.run_id.startswith("rep")
