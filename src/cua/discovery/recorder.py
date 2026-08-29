"""Recording what actually happened.

The distinction this module exists to enforce: **the artifact is compiled from what
executed and was verified, not from the model's narration.** The transcript is evidence;
the recording is ground truth. Keeping them separate is why replay does not inherit the
model's mistakes - a step the model described but that changed nothing on screen is
simply not in the recording, so it cannot reach the artifact.

Each entry holds the action, the locator ladder generated at the moment of the act, and
the observations either side of it. That before/after pair is what lets the compiler
synthesise a checkpoint later: whatever appeared that was not there before is, by
construction, evidence the step worked.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from cua.locator.generate import CONTENT_ROLES, generate
from cua.schema.capability import ActionKind
from cua.schema.locator import Locator
from cua.surface.base import ElementNode, Observation


class RecordedAction(BaseModel):
    """One executed action, with everything needed to compile a step from it."""

    seq: int
    action: ActionKind
    #: The model's stated reason. Becomes `Step.intent`, and is the only part of the
    #: model's narration that survives into the artifact.
    why: str
    target: Locator | None = None
    element_describe: str = ""
    control_name: str = ""
    value_literal: str | None = None
    is_parameter: bool = False
    parameter_name: str = ""
    #: Typed into a password field. The literal must never reach the artifact;
    #: the compiler turns it into a `secret_ref` resolved at execution time.
    is_secret: bool = False
    secret_ref: str = ""
    url: str | None = None
    output_name: str | None = None
    output_type: str = "text"
    output_sensitive: bool = False

    observation_before: str = Field(default="", description="Fingerprint before acting.")
    observation_after: str = Field(default="", description="Fingerprint after acting.")
    url_before: str = ""
    url_after: str = ""
    #: Elements present after but not before. The raw material for a post-condition.
    appeared: list[str] = Field(default_factory=list)
    succeeded: bool = True
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def changed_state(self) -> bool:
        """Did this action do anything at all?

        A step that left the screen fingerprint and the URL untouched accomplished
        nothing, and the compiler prunes it. Typing is the deliberate exception: filling
        a field is real progress even though the screen's *shape* is unchanged, which is
        precisely why the fingerprint ignores values.
        """
        if self.action in (ActionKind.FILL, ActionKind.SELECT, ActionKind.EXTRACT):
            return True
        return (
            self.observation_before != self.observation_after or self.url_before != self.url_after
        )


class RecordedCheckpoint(BaseModel):
    """A screen the model declared as significant."""

    seq: int
    describe: str
    locators: list[Locator] = Field(default_factory=list)
    fingerprint: str = ""
    url: str = ""


class RecordedOutcome(BaseModel):
    """An exceptional-but-legitimate screen encountered during discovery.

    Seeds `known_outcomes`. Recording these during discovery is how a capability learns
    that "no such member" is an answer before anyone hits it in production.
    """

    code: str
    describe: str
    text_pattern: str = ""
    fingerprint: str = ""


class Recorder:
    """Accumulates the ground truth of a discovery run."""

    def __init__(self) -> None:
        self.actions: list[RecordedAction] = []
        self.checkpoints: list[RecordedCheckpoint] = []
        self.outcomes: list[RecordedOutcome] = []
        self.entry_url: str = ""
        #: The shape of the *entry* screen. Drift is checked at the point a run
        #: starts, so the recorded and observed fingerprints have to be of the same
        #: screen - comparing the entry screen against the last one recorded means
        #: every single replay reports drift, and a warning that is always on is
        #: worse than none.
        self.entry_fingerprint: str = ""
        self.final_fingerprint: str = ""
        self._seq = 0

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def record_action(
        self,
        *,
        action: ActionKind,
        why: str,
        before: Observation,
        after: Observation,
        element: ElementNode | None = None,
        value: str | None = None,
        is_parameter: bool = False,
        parameter_name: str = "",
        is_secret: bool = False,
        secret_ref: str = "",
        url: str | None = None,
        output_name: str | None = None,
        output_type: str = "text",
        output_sensitive: bool = False,
        succeeded: bool = True,
    ) -> RecordedAction:
        locator = (
            generate(element, before, for_extraction=action is ActionKind.EXTRACT)
            if element is not None
            else None
        )

        before_names = {(n.role, n.name) for n in before.elements}
        appeared = [
            f"{n.role}:{n.name}"
            for n in after.elements
            if (n.role, n.name) not in before_names and n.name
        ]

        entry = RecordedAction(
            seq=self._next(),
            action=action,
            why=why,
            target=locator,
            element_describe=element.describe() if element else "",
            control_name=_control_name(element, action),
            value_literal=value,
            is_parameter=is_parameter,
            parameter_name=parameter_name,
            is_secret=is_secret,
            secret_ref=secret_ref,
            url=url,
            output_name=output_name,
            output_type=output_type,
            output_sensitive=output_sensitive,
            observation_before=before.fingerprint,
            observation_after=after.fingerprint,
            url_before=before.url,
            url_after=after.url,
            appeared=appeared[:12],
            succeeded=succeeded,
        )
        self.actions.append(entry)
        self.final_fingerprint = after.fingerprint
        return entry

    def record_checkpoint(
        self, describe: str, elements: list[ElementNode], observation: Observation
    ) -> None:
        self.checkpoints.append(
            RecordedCheckpoint(
                seq=self._next(),
                describe=describe,
                locators=[generate(e, observation) for e in elements],
                fingerprint=observation.fingerprint,
                url=observation.url,
            )
        )

    def record_outcome(
        self, code: str, describe: str, text_pattern: str, observation: Observation
    ) -> None:
        if any(o.code == code for o in self.outcomes):
            return
        self.outcomes.append(
            RecordedOutcome(
                code=code,
                describe=describe,
                text_pattern=text_pattern,
                fingerprint=observation.fingerprint,
            )
        )

    @property
    def effective_actions(self) -> list[RecordedAction]:
        """Actions that succeeded and actually changed something.

        This is the prune. A model that clicked a dead link, went back, and tried again
        leaves three entries here and contributes one step to the artifact.
        """
        return [a for a in self.actions if a.succeeded and a.changed_state]

    @property
    def parameters(self) -> dict[str, str]:
        """parameter name -> the literal value used during discovery.

        Needed by the compiler to parameterise locators: a tier-4 row key recorded as
        "Member ID = 100042" has to become "Member ID = {{memberId}}" or the capability
        works for exactly one member.
        """
        return {
            a.parameter_name: a.value_literal or ""
            for a in self.actions
            if a.is_parameter and a.parameter_name and a.value_literal and not a.is_secret
        }


def _control_name(element: ElementNode | None, action: ActionKind) -> str:
    """What to record as the name of the control a step acts on.

    For an extraction target whose accessible name *is* its content - a table cell, a
    definition list value - the name is the balance being read. Putting that in the
    artifact would commit a member's regulated data to git, and would be circular
    besides. The column header or preceding label identifies the same control without
    quoting what it currently says.
    """
    if element is None:
        return ""
    if action is ActionKind.EXTRACT and element.role in CONTENT_ROLES:
        column = element.row_context.column_header if element.row_context else ""
        return column or element.anchor_text or ""
    return element.name
