"""Classifying how dangerous an action is.

The judgement is made from **what the control says**, because that is what a human
operator reads before deciding. A button labelled "Confirm and Open Account" is
irreversible whatever markup it happens to be built from, and no amount of DOM
inspection would tell you that more reliably than the words on it.

Three classes, and the boundaries are drawn where a caller's response differs:

    read_only          nothing changed; safe to retry, safe to run unattended
    reversible_write   state changed but can be undone; needs approval, retryable
    irreversible_write money moved or a record was created; approval, and never a
                       blind retry

Typing is deliberately `read_only`. Filling a field commits nothing - it is the submit
that does. Treating every keystroke as a write would make the risk signal useless by
firing on every step of every form.
"""

from __future__ import annotations

import re
from functools import lru_cache

from cua.schema.capability import ActionKind, RiskClass

#: Actions that cannot, by themselves, change anything in the target application.
_READ_ONLY_ACTIONS = frozenset(
    {
        ActionKind.NAVIGATE,
        ActionKind.EXTRACT,
        ActionKind.ASSERT,
        ActionKind.WAIT,
        ActionKind.FILL,
        ActionKind.SELECT,
    }
)


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _matches_any(text: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        try:
            if _compiled(pattern).search(text):
                return pattern
        except re.error:
            continue
    return None


def classify(
    action: ActionKind,
    control_name: str,
    *,
    irreversible_signals: list[str],
    reversible_signals: list[str],
    declared: RiskClass | None = None,
) -> tuple[RiskClass, str]:
    """Return the risk class and the reason, for the audit trail.

    `declared` is the artifact's own assessment. It can only make the classification
    *stricter*: an author may mark a step irreversible that the signals missed, but
    cannot mark a step labelled "Confirm and Open Account" as read-only. Authors are
    trusted to add caution, not to remove it.
    """
    inferred, reason = _infer(action, control_name, irreversible_signals, reversible_signals)

    if declared is not None and _rank(declared) > _rank(inferred):
        return declared, f"declared {declared} in the artifact (stricter than inferred)"
    return inferred, reason


def _infer(
    action: ActionKind,
    control_name: str,
    irreversible_signals: list[str],
    reversible_signals: list[str],
) -> tuple[RiskClass, str]:
    if action in _READ_ONLY_ACTIONS:
        return RiskClass.READ_ONLY, f"{action} cannot change application state"

    name = (control_name or "").strip()
    if not name:
        # A click on something we cannot name is not obviously safe. Legacy apps have
        # plenty of unlabelled controls, and guessing "harmless" on one that turns out
        # to post a transaction is the wrong direction to be wrong in.
        return RiskClass.REVERSIBLE_WRITE, "click on an unnamed control; assumed to write"

    hit = _matches_any(name, tuple(irreversible_signals))
    if hit:
        return RiskClass.IRREVERSIBLE_WRITE, f"control name {name!r} matches {hit}"

    hit = _matches_any(name, tuple(reversible_signals))
    if hit:
        return RiskClass.REVERSIBLE_WRITE, f"control name {name!r} matches {hit}"

    return RiskClass.READ_ONLY, f"control name {name!r} matches no write signal"


_ORDER = {
    RiskClass.READ_ONLY: 0,
    RiskClass.REVERSIBLE_WRITE: 1,
    RiskClass.IRREVERSIBLE_WRITE: 2,
}


def _rank(risk: RiskClass) -> int:
    return _ORDER[risk]
