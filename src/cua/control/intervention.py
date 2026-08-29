"""Routing a stuck run to a human.

An intervention is the message that crosses the seam. Locally it is a file and a URL
printed to stdout; in production it is a queue message or a webhook to an ops console.
**The payload is identical either way**, which is the point of defining it as a model
rather than as a print statement - swapping the transport does not change what an
operator receives.

What it has to carry, from the brief: *"which capability/goal, the current step, the
current state or screenshot, and why it stopped."* All four are here, plus the two
things an operator actually needs to act rather than merely understand: what the system
already tried, and what it thinks the reasonable options are.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from cua.control.session import Disposition, HumanAction
from cua.schema.result import EscalationReason

if TYPE_CHECKING:
    from cua.policy.redactor import Redactor


class InterventionStatus(StrEnum):
    OPEN = "open"
    #: An operator has the lease and is working in the live session.
    TAKEN = "taken"
    RESOLVED = "resolved"
    ABANDONED = "abandoned"


class Intervention(BaseModel):
    id: str
    run_id: str
    raised_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    capability: str
    goal: str = ""
    tenant: str | None = None

    step_id: str | None = None
    step_intent: str = Field(
        default="",
        description="What automation was trying to do, in the words recorded at "
        "discovery. An operator should not have to read JSON to find out.",
    )
    reason: EscalationReason
    explain: str = Field(
        default="",
        description="Why it stopped, in a sentence. The difference between 'a dialog "
        "appeared' and 'a dialog titled X appeared and is not declared in this "
        "capability's recoveries' is the difference between a ticket and a fix.",
    )
    attempted: list[str] = Field(
        default_factory=list,
        description="What was already tried, so the operator does not repeat it.",
    )

    url: str = ""
    screenshot_path: str | None = None
    aria_snapshot: str = Field(
        default="",
        description="What the *system* perceived. Shown next to the screenshot because "
        "the difference between the two is usually the explanation.",
    )
    suggested_actions: list[str] = Field(default_factory=list)

    status: InterventionStatus = InterventionStatus.OPEN
    operator: str | None = None
    taken_at: datetime | None = None
    resolved_at: datetime | None = None
    disposition: Disposition | None = None
    note: str = ""
    human_actions: list[str] = Field(default_factory=list)

    operator_url: str = ""

    def summary(self) -> str:
        return (
            f"{self.reason} at {self.step_id or '?'} ({self.step_intent or 'no recorded intent'})"
        )


class InterventionStore:
    """In-memory, mirrored to disk.

    Deliberately not a database. Interventions are short-lived coordination state for a
    single run, and the durable record is the evidence directory - which has to exist
    anyway. In production this becomes a queue; `InterventionStore` is the interface that
    would grow a second implementation, not a design that needs rewriting.
    """

    def __init__(self, evidence_dir: Path | None = None, redactor: Redactor | None = None) -> None:
        self._items: dict[str, Intervention] = {}
        self._dir = evidence_dir
        #: Applied on *persist* only. The operator sees the live screen through an
        #: authenticated console and needs the real values to do their job; the file left
        #: behind afterwards is committed evidence and must not carry them.
        self._redactor = redactor

    def raise_(self, intervention: Intervention) -> Intervention:
        self._items[intervention.id] = intervention
        self._persist(intervention)
        return intervention

    def get(self, intervention_id: str) -> Intervention | None:
        return self._items.get(intervention_id)

    def open_items(self) -> list[Intervention]:
        return [
            i
            for i in self._items.values()
            if i.status in (InterventionStatus.OPEN, InterventionStatus.TAKEN)
        ]

    def all_items(self) -> list[Intervention]:
        return sorted(self._items.values(), key=lambda i: i.raised_at)

    def take(self, intervention_id: str, operator: str) -> Intervention | None:
        item = self._items.get(intervention_id)
        if item is None or item.status is not InterventionStatus.OPEN:
            return None
        item.status = InterventionStatus.TAKEN
        item.operator = operator
        item.taken_at = datetime.now(UTC)
        self._persist(item)
        return item

    def resolve(
        self,
        intervention_id: str,
        disposition: Disposition,
        *,
        note: str = "",
        actions: list[HumanAction] | None = None,
        operator: str | None = None,
    ) -> Intervention | None:
        """`operator` is a fallback for a resolution that never went through `take`.

        The console always takes before it resumes, so normally the name is already on
        the record. An authorisation raised during discovery need not - answering "yes,
        open the account" is a decision rather than a takeover - and an audit record of
        an irreversible action that does not name the human who authorised it is the one
        record in this system that would be worthless.
        """
        item = self._items.get(intervention_id)
        if item is None:
            return None
        if operator and not item.operator:
            item.operator = operator
        item.status = InterventionStatus.RESOLVED
        item.resolved_at = datetime.now(UTC)
        item.disposition = disposition
        item.note = note
        item.human_actions = [a.describe() for a in (actions or [])]
        self._persist(item)
        return item

    def _persist(self, item: Intervention) -> None:
        if self._dir is None:
            return
        target = self._dir / "interventions"
        target.mkdir(parents=True, exist_ok=True)
        payload = item.model_dump(mode="json")
        if self._redactor is not None:
            payload = self._redactor.value(payload)
        (target / f"{item.id}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def suggest(reason: EscalationReason) -> list[str]:
    """Sensible options for an operator, per reason.

    Every list ends with abort. An operator who cannot safely finish must always have a
    way out that is not "close the tab and hope".
    """
    common = ["Abort the run"]
    if reason is EscalationReason.UNKNOWN_DIALOG:
        return [
            "Read the dialog, dismiss it if it is safe, then resume as 'recovered'",
            "Complete the step yourself and resume as 'step completed'",
            *common,
        ]
    if reason is EscalationReason.RISKY_STEP_NEEDS_APPROVAL:
        return [
            "Perform the irreversible step yourself and resume as 'step completed'",
            *common,
        ]
    if reason is EscalationReason.AMBIGUOUS_WRITE_OUTCOME:
        return [
            "Check whether the write landed before doing anything else - retrying "
            "could post it twice",
            "If it did land, resume as 'step completed'",
            "If it did not, resume as 'recovered' to retry the step",
            *common,
        ]
    if reason is EscalationReason.RECOVERY_EXHAUSTED:
        return [
            "Clear the condition manually, then resume as 'recovered'",
            "Finish the flow by hand and resume as 'flow completed'",
            *common,
        ]
    return [
        "Take control, look at the screen, and put the session into the expected state",
        "Resume as 'recovered' to retry the step",
        *common,
    ]
