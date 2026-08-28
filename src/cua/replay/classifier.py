"""Deciding what a screen means.

This is where the brief's central distinction is actually enforced. After every action
the replay engine hands the new observation here, and the answer is one of four things:

    business outcome   a legitimate answer the caller asked for. NOT an error.
    recoverable        a transient or known condition we know how to get past
    escalate           unsafe to guess; a human decides
    clean              nothing notable; carry on and check the step's own assertions

Evaluation order matters and is deliberate:

1. **The artifact's own `known_outcomes`** first, so a capability's declared results win
   over any generic interpretation. "No records found" is what this capability *asked
   about*, not a failure of it.
2. **The artifact's `recoveries`**, bounded.
3. **Unknown modal dialogs.** A dialog nobody declared is the clearest "stop and ask"
   signal a UI produces - it is a question the application is putting to an operator,
   and answering it by guessing is exactly the thing that must not happen in a bank.

Note what is *not* here: HTTP status codes. Detection works from what is on screen,
because the failures that matter in these applications render as HTTP 200 with a red
sentence on the page. A classifier that watched status codes would miss every one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from cua.replay.assertions import evaluate
from cua.schema.capability import Capability, KnownOutcome, Recovery
from cua.schema.result import EscalationReason
from cua.surface.base import Observation


class StateClass(StrEnum):
    CLEAN = "clean"
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    ESCALATE = "escalate"


@dataclass
class Classification:
    state: StateClass
    outcome: KnownOutcome | None = None
    recovery: Recovery | None = None
    escalation_reason: EscalationReason | None = None
    detail: str = ""
    evidence: list[str] = field(default_factory=list)


class StateClassifier:
    """Stateless apart from recovery attempt counts, which have to be per-run."""

    def __init__(self, capability: Capability) -> None:
        self._capability = capability
        self._recovery_attempts: dict[str, int] = {}

    def classify(self, observation: Observation) -> Classification:
        for outcome in self._capability.known_outcomes:
            result = evaluate(outcome.detect, observation)
            if not result.passed:
                continue
            if outcome.terminal:
                return Classification(
                    state=StateClass.BUSINESS_OUTCOME,
                    outcome=outcome,
                    detail=outcome.message,
                    evidence=[result.describe],
                )
            # Non-terminal outcomes (session expiry) are conditions to get past, not
            # answers to return. They are declared as outcomes so they appear in the
            # contract, but handled as recoveries.
            recovery = self._recovery_for(outcome.code)
            if recovery is not None:
                return self._attempt(recovery, f"{outcome.code}: {outcome.message}")
            return Classification(
                state=StateClass.ESCALATE,
                escalation_reason=EscalationReason.RECOVERY_EXHAUSTED,
                detail=f"{outcome.code} has no recovery declared",
            )

        for recovery in self._capability.recoveries:
            if evaluate(recovery.detect, observation).passed:
                return self._attempt(recovery, recovery.describe)

        unknown = self._unknown_dialog(observation)
        if unknown is not None:
            return Classification(
                state=StateClass.ESCALATE,
                escalation_reason=EscalationReason.UNKNOWN_DIALOG,
                detail=(
                    f"a dialog titled {unknown!r} appeared and is not declared in this "
                    f"capability's recoveries or known outcomes"
                ),
                evidence=[unknown],
            )

        return Classification(state=StateClass.CLEAN)

    # ------------------------------------------------------------------ internals

    def _attempt(self, recovery: Recovery, detail: str) -> Classification:
        used = self._recovery_attempts.get(recovery.id, 0)
        if used >= recovery.max_attempts:
            # Bounded, always. An unbounded retry loop is how "deterministic" quietly
            # becomes "eventually consistent", and in a write flow it is how you
            # double-post.
            return Classification(
                state=StateClass.ESCALATE,
                escalation_reason=EscalationReason.RECOVERY_EXHAUSTED,
                detail=f"{recovery.id} exhausted after {used} attempt(s): {detail}",
            )
        self._recovery_attempts[recovery.id] = used + 1
        return Classification(state=StateClass.RECOVERABLE, recovery=recovery, detail=detail)

    def attempts_for(self, recovery_id: str) -> int:
        return self._recovery_attempts.get(recovery_id, 0)

    def _recovery_for(self, code: str) -> Recovery | None:
        wanted = code.lower().replace("_", "-")
        for recovery in self._capability.recoveries:
            if wanted in recovery.id.lower():
                return recovery
        return None

    def _unknown_dialog(self, observation: Observation) -> str | None:
        """A modal nobody declared.

        The application is asking an operator a question. Dismissing it because it looks
        harmless is precisely the class of decision this system is not allowed to make -
        so an undeclared dialog escalates, every time.
        """
        for node in observation.elements:
            if node.role != "dialog" or not node.name:
                continue
            if not self._is_declared(node.name):
                return node.name
        return None

    def _is_declared(self, title: str) -> bool:
        from cua.replay.assertions import _search

        for recovery in self._capability.recoveries:
            if recovery.detect.pattern and _search(recovery.detect.pattern, title):
                return True
        for outcome in self._capability.known_outcomes:
            if outcome.detect.pattern and _search(outcome.detect.pattern, title):
                return True
        return False
