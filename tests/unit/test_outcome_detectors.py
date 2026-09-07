"""The declared business-outcome detectors, checked against the strings the app renders.

A `known_outcome` is only worth its place in the contract if its detector actually fires
on the screen it claims to recognise. Both halves of that failed here at different times
and neither showed up in a browser test, because the eval matrix never exercised
`VALIDATION_REJECTED` at all:

* the detector matched only the member-search messages the recorded flow happened to
  show, so "Account nickname is required." fell straight through the classifier and would
  have reached the caller as a checkpoint failure - a legitimate answer reported as a
  crash, which is the exact mistake the taxonomy exists to prevent;
* widening it to a bare `is required` then matched the maintenance interstitial, which
  says "No action is required" - classifying a dismissible notice as a terminal rejection
  and *inverting its meaning*.

So these are unit tests over the patterns and the literal strings from `targetapp`, not
integration tests. A regex is cheap to get subtly wrong and expensive to debug through a
browser, and the false-positive direction matters as much as the false-negative one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CAPABILITIES = Path(__file__).resolve().parents[2] / "capabilities"

#: Rendered by `targetapp` on a validation failure. Copied verbatim, on purpose: if a
#: message changes there and nobody updates the detector, this test is where it surfaces.
SHOULD_MATCH = [
    "Account nickname is required.",
    "Branch selection is required.",
    "Invalid Member ID. Enter a 6-digit numeric member number.",
]

#: Also real screens. Each one would be actively harmful to classify as a validation
#: rejection, because each means something different and some mean the opposite.
SHOULD_NOT_MATCH = [
    # A recoverable notice. Matching this terminates a run that should carry on.
    "Scheduled maintenance window this weekend. No action is required.",
    # A missing credential is a configuration fault on our side, not the application
    # rejecting a caller's data, so it must fail loudly rather than return an answer.
    # Note the plural: the field validations above all say "is required".
    "User ID and password are required.",
    "Regulation CC Hold Notice",
    "Server Error in '/' Application",
    "No member exists with that identifier.",
    "Savings Balance 4,182.55",
]


def _validation_patterns() -> list[tuple[str, str]]:
    out = []
    for path in sorted(CAPABILITIES.glob("*.json")):
        capability = json.loads(path.read_text(encoding="utf-8"))
        for outcome in capability["known_outcomes"]:
            if outcome["code"] == "VALIDATION_REJECTED":
                out.append((path.name, outcome["detect"]["pattern"]))
    assert out, "no capability declares VALIDATION_REJECTED"
    return out


@pytest.mark.parametrize("text", SHOULD_MATCH)
def test_the_validation_detector_fires_on_what_the_app_actually_renders(text: str) -> None:
    for name, pattern in _validation_patterns():
        assert re.search(pattern, text), f"{name}: missed {text!r}"


@pytest.mark.parametrize("text", SHOULD_NOT_MATCH)
def test_the_validation_detector_does_not_fire_on_anything_else(text: str) -> None:
    """The direction that bit. A detector that over-matches does not merely mislabel - it
    converts a recoverable condition into a terminal one and stops a healthy run."""
    for name, pattern in _validation_patterns():
        assert not re.search(pattern, text), f"{name}: false positive on {text!r}"


def test_every_declared_outcome_has_a_detector_that_could_fire() -> None:
    """A code with an empty or absent pattern is a promise in the contract with nothing
    behind it - the caller is told the outcome can happen and it never will."""
    for path in sorted(CAPABILITIES.glob("*.json")):
        capability = json.loads(path.read_text(encoding="utf-8"))
        for outcome in capability["known_outcomes"]:
            detect = outcome["detect"]
            assert detect["kind"], f"{path.name}/{outcome['code']}: no detector kind"
            if detect["kind"] in ("text_present", "text_absent", "url_matches"):
                assert detect.get("pattern"), (
                    f"{path.name}/{outcome['code']}: {detect['kind']} with no pattern"
                )
                re.compile(detect["pattern"])  # a broken regex fails the build, not a run


def test_capabilities_the_matrix_approves_are_actually_approved() -> None:
    """A scenario that passes `--approve` still needs the capability itself approved.

    Both gates are deliberate - risk is a property of the action, status is how far this
    recording is trusted - so a capability sitting at `draft` turns every such scenario
    into `blocked`. That is correct behaviour and a broken matrix.

    It happened: the demo script resets the capability to draft so a walkthrough shows
    both gates falling away, and a `git add -A` swept that reset into a commit. Four
    commits shipped with an artifact that would have failed three scenarios in CI.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from cua.app.evaluate import SCENARIOS

    needed = {s.capability for s in SCENARIOS if s.approve}
    for capability_id in sorted(needed):
        matches = sorted(CAPABILITIES.glob(f"{capability_id}@*.json"))
        assert matches, f"{capability_id} is exercised by the matrix but not committed"
        status = json.loads(matches[-1].read_text(encoding="utf-8"))["status"]
        assert status == "approved", (
            f"{matches[-1].name} is '{status}'. The matrix runs scenarios against it with "
            f"--approve, which also requires status == approved, so those rows would come "
            f"back `blocked`. Run: cua approve {capability_id} --reviewer <you>"
        )
