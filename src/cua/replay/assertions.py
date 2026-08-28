"""Evaluating checkpoints.

Pure functions over an `Observation`. No browser, no I/O - which means every assertion
type, and therefore the detector behind every business outcome, is unit-testable in
milliseconds. Given that the difference between "no such member" and "the automation
broke" is decided here, that testability is worth designing for.

From the brief's glossary, a checkpoint is *"a condition you assert to confirm you
actually reached the state you expected, rather than assuming the click worked."* The
practical consequence is per-step assertions rather than one at the end: that is what
turns "the run failed" into "step s4 expected the member detail screen, observed a
sign-in form".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch

from cua.locator.match import resolve
from cua.schema.capability import Assertion, AssertionKind
from cua.surface.base import Observation


@dataclass(frozen=True)
class AssertionResult:
    passed: bool
    describe: str
    observed: str = ""

    def __bool__(self) -> bool:
        return self.passed


def screen_text(observation: Observation) -> str:
    """Everything a human would read on this screen, as one searchable string.

    Built from accessible names and values rather than from markup, so a text detector
    matches what is *displayed* - not a string buried in an attribute or a comment.
    """
    parts: list[str] = []
    for node in observation.elements:
        if node.name:
            parts.append(node.name)
        if node.value:
            parts.append(node.value)
    return "\n".join(parts)


def evaluate(assertion: Assertion, observation: Observation) -> AssertionResult:
    kind = assertion.kind
    label = assertion.describe or str(kind)

    if kind is AssertionKind.ALL_OF:
        results = [evaluate(a, observation) for a in assertion.of]
        failed = [r for r in results if not r.passed]
        return AssertionResult(
            passed=not failed,
            describe=label,
            observed="; ".join(r.describe for r in failed[:3]) if failed else "",
        )

    if kind is AssertionKind.ANY_OF:
        results = [evaluate(a, observation) for a in assertion.of]
        return AssertionResult(
            passed=any(r.passed for r in results),
            describe=label,
            observed="" if any(r.passed for r in results) else "none of the alternatives held",
        )

    if kind in (AssertionKind.TEXT_PRESENT, AssertionKind.TEXT_ABSENT):
        found = _search(assertion.pattern or "", screen_text(observation))
        want = kind is AssertionKind.TEXT_PRESENT
        return AssertionResult(
            passed=found is want,
            describe=label,
            observed=f"pattern {'found' if found else 'not found'} on screen",
        )

    if kind is AssertionKind.URL_MATCHES:
        pattern = assertion.pattern or ""
        matched = fnmatch(observation.url, pattern) or bool(_search(pattern, observation.url))
        return AssertionResult(passed=matched, describe=label, observed=f"url is {observation.url}")

    if kind in (AssertionKind.ELEMENT_PRESENT, AssertionKind.ELEMENT_ABSENT):
        if assertion.locator is None:
            return AssertionResult(False, label, "assertion has no locator")
        resolution = resolve(assertion.locator, observation)
        present = resolution.ref is not None
        want = kind is AssertionKind.ELEMENT_PRESENT
        return AssertionResult(
            passed=present is want,
            describe=label,
            observed=(
                f"{assertion.locator.describe}: {resolution.outcome}"
                f"{f' (tier {resolution.winning_tier})' if resolution.winning_tier else ''}"
            ),
        )

    return AssertionResult(False, label, f"unsupported assertion kind {kind}")


def evaluate_all(
    assertions: list[Assertion], observation: Observation
) -> tuple[bool, list[AssertionResult]]:
    results = [evaluate(a, observation) for a in assertions]
    return all(r.passed for r in results), results


def _search(pattern: str, haystack: str) -> bool:
    if not pattern:
        return False
    try:
        return re.search(pattern, haystack, re.IGNORECASE | re.MULTILINE) is not None
    except re.error:
        # A malformed detector must not take the run down with it. Treating it as
        # "did not match" is the safe direction: an outcome goes unrecognised and
        # escalates, rather than being falsely claimed.
        return pattern.lower() in haystack.lower()
