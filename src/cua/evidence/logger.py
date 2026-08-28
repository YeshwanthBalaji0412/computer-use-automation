"""Run evidence.

The brief asks for "a structured log of what the agent did **and why**", plus at least
one richer signal on failure. The `why` is not an afterthought here: during discovery the
model must supply a `why` with every tool call, and that string becomes the step's
`intent` in the artifact and the label in this log. A trace that says *"clicked e11"* is
useless six weeks later; *"submit the member search"* is not.

Layout, one directory per run:

    evidence/runs/<run_id>/
        manifest.json      what ran, with what inputs (redacted), and how it ended
        events.jsonl       one JSON object per event, in order
        steps/*.png        screenshots, masked before encoding
        failure/           on failure: screenshot + aria snapshot + last observations
        trace.zip          Playwright trace - openable in the trace viewer
        report.md          human-readable summary

Everything written here passes through the `Redactor` first. There is no second code
path to disk, which is what makes "no regulated data is persisted" a property of the
design rather than a habit.

The `actor` field on every event distinguishes automation from a human operator, so the
handoff audit trail falls out of the same stream rather than needing its own.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from cua.policy.redactor import Redactor

Actor = Literal["automation", "human", "system", "model"]


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    OBSERVED = "observed"
    ACTION = "action"
    STEP_STARTED = "step_started"
    STEP_FINISHED = "step_finished"
    ASSERTION = "assertion"
    LOCATOR_RESOLVED = "locator_resolved"
    #: A better-ranked strategy stopped working. Not a failure; the drift warning.
    LOCATOR_DEGRADED = "locator_degraded"
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERY = "recovery"
    POLICY_DECISION = "policy_decision"
    ESCALATION_RAISED = "escalation_raised"
    CONTROL_TRANSFERRED = "control_transferred"
    HUMAN_ACTION = "human_action"
    MODEL_TURN = "model_turn"
    ERROR = "error"


class EvidenceLogger:
    """Writes one run's evidence. Cheap to construct, safe to use for a whole run."""

    def __init__(
        self,
        root: Path,
        *,
        kind: Literal["discovery", "replay", "verify"],
        redactor: Redactor,
        run_id: str | None = None,
    ) -> None:
        self.run_id = run_id or f"{kind[:3]}_{uuid.uuid4().hex[:12]}"
        self.kind = kind
        self.dir = root / "runs" / self.run_id
        self.steps_dir = self.dir / "steps"
        self.failure_dir = self.dir / "failure"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.steps_dir.mkdir(exist_ok=True)

        self._redactor = redactor
        self._events_path = self.dir / "events.jsonl"
        self._seq = 0
        self._started = time.monotonic()

    # ------------------------------------------------------------------ events

    def event(
        self,
        type_: EventType,
        *,
        actor: Actor = "automation",
        step_id: str | None = None,
        **payload: Any,
    ) -> None:
        self._seq += 1
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "seq": self._seq,
            "run_id": self.run_id,
            "actor": actor,
            "type": str(type_),
            "step_id": step_id,
            "payload": self._redactor.value(payload),
        }
        with self._events_path.open("a", encoding="utf-8") as fh:
            # ensure_ascii=False so redaction markers stay readable as «redacted:x» rather
            # than \u00ab escapes. Evidence nobody can read is evidence nobody checks.
            fh.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")

    def register_secret(self, label: str, value: str) -> None:
        """Mask this value everywhere from now on.

        Called the moment a secret is typed, before anything is written. Registration
        rather than filtering at each call site: a secret that is only masked where
        someone remembered to mask it is not masked.
        """
        self._redactor.register(label, value)

    def error(self, message: str, **payload: Any) -> None:
        self.event(EventType.ERROR, level="error", message=message, **payload)

    # ------------------------------------------------------------------ artefacts

    def step_screenshot_path(self, step_id: str, phase: Literal["pre", "post"]) -> Path:
        return self.steps_dir / f"{step_id}-{phase}.png"

    def write_failure_context(
        self,
        *,
        aria_snapshot: str,
        observations: list[dict[str, Any]],
        summary: str,
    ) -> Path:
        """The richer signal the brief asks for on failure.

        A screenshot alone shows what it looked like; the aria snapshot shows what the
        system *perceived*, and the difference between those two is usually the bug.
        """
        self.failure_dir.mkdir(exist_ok=True)
        (self.failure_dir / "aria.txt").write_text(
            self._redactor.text(aria_snapshot), encoding="utf-8"
        )
        (self.failure_dir / "observations.json").write_text(
            json.dumps(
                self._redactor.value(observations), indent=2, default=str, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        (self.failure_dir / "summary.txt").write_text(
            self._redactor.text(summary), encoding="utf-8"
        )
        return self.failure_dir

    def write_manifest(self, **fields: Any) -> Path:
        manifest = {
            "run_id": self.run_id,
            "kind": self.kind,
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "duration_ms": int((time.monotonic() - self._started) * 1000),
            **self._redactor.value(fields),
        }
        path = self.dir / "manifest.json"
        path.write_text(
            json.dumps(manifest, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
        )
        return path

    def read_events(self) -> list[dict[str, Any]]:
        if not self._events_path.exists():
            return []
        return [
            json.loads(line)
            for line in self._events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    # ------------------------------------------------------------------ report

    def write_report(self, title: str) -> Path:
        """A readable summary, so a reviewer does not have to parse JSONL by eye."""
        events = self.read_events()
        lines = [
            f"# {title}",
            "",
            f"- run: `{self.run_id}` ({self.kind})",
            f"- events: {len(events)}",
            "",
            "| # | actor | event | step | detail |",
            "|---|-------|-------|------|--------|",
        ]
        for ev in events:
            detail = ev["payload"].get("why") or ev["payload"].get("intent") or ""
            if not detail:
                detail = ", ".join(
                    f"{k}={v}" for k, v in list(ev["payload"].items())[:2] if v is not None
                )
            lines.append(
                f"| {ev['seq']} | {ev['actor']} | {ev['type']} | "
                f"{ev.get('step_id') or ''} | {str(detail)[:90]} |"
            )

        path = self.dir / "report.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
