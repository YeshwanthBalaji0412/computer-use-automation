"""Redaction.

Two things are being proved: that regulated data is masked, and - just as important -
that ordinary identifiers are *not*. A redactor that masks everything makes evidence
unreadable, which means nobody reads it, which means it protects nothing in practice.
"""

from __future__ import annotations

import json

import pytest

from cua.policy.redactor import Redactor
from cua.schema.capability import (
    Capability,
    InputParam,
    OutputField,
    ParamType,
    ProductRef,
    Sensitivity,
    TargetRef,
)
from cua.schema.policy import RedactionPolicy

pytestmark = pytest.mark.unit


@pytest.fixture
def redactor() -> Redactor:
    return Redactor(salt="test-salt")


# ------------------------------------------------------------------ patterns


@pytest.mark.parametrize(
    "text,label",
    [
        ("SSN on file: 123-45-6789", "ssn"),
        ("contact j.rivera@example.com for details", "email"),
        ("call (617) 555-0134 before noon", "phone"),
        ("date of birth 03/14/1986", "dob"),
        ("routing 021000021456789", "account"),
    ],
)
def test_regulated_patterns_are_masked(redactor: Redactor, text: str, label: str) -> None:
    out = redactor.text(text)
    assert f"«redacted:{label}»" in out


def test_card_numbers_are_luhn_checked(redactor: Redactor) -> None:
    """The false-positive guard that makes the card rule usable.

    4111 1111 1111 1111 is a valid test PAN. 4111 1111 1111 1112 is not - it is just a
    sixteen-digit reference, and masking it would be noise.
    """
    assert "«redacted:card»" in redactor.text("card 4111111111111111 on file")
    masked_invalid = redactor.text("reference 4111111111111112 recorded")
    assert "«redacted:card»" not in masked_invalid


def test_internal_identifiers_stay_readable(redactor: Redactor) -> None:
    """Member ids and confirmation numbers are internal identifiers, not PII. Masking
    them would make every log useless for debugging and buy nothing."""
    text = "member 100042 -> confirmation SA-481920, status ACTIVE"
    assert redactor.text(text) == text


def test_currency_is_not_mistaken_for_an_account_number(redactor: Redactor) -> None:
    assert redactor.text("balance $4,182.55") == "balance $4,182.55"


# ------------------------------------------------------------------ declared fields


def capability_with(sensitivity: Sensitivity) -> Capability:
    return Capability(
        id="member.savings-balance",
        version="1.0.0",
        display_name="Read savings balance",
        description="test",
        target=TargetRef(
            product=ProductRef(vendor="corelink", app="servicing-console"),
            entry_url_pattern="{{base_url}}/search",
        ),
        inputs=[InputParam(name="memberId", sensitivity=sensitivity, example="100042")],
        outputs=[
            OutputField(
                name="savingsBalance",
                type=ParamType.CURRENCY,
                sensitivity=Sensitivity.PII,
            )
        ],
    )


def test_declared_sensitive_inputs_are_masked_by_name(redactor: Redactor) -> None:
    """The primary mechanism. Precise and complete for anything the schema knows about -
    which is every input and output of every capability."""
    redactor.register_capability(capability_with(Sensitivity.PII), {"memberId": "100042"})
    assert "«redacted:memberId»" in redactor.text("looked up member 100042 on screen")


def test_non_sensitive_inputs_are_left_alone(redactor: Redactor) -> None:
    redactor.register_capability(capability_with(Sensitivity.INTERNAL), {"memberId": "100042"})
    assert redactor.text("looked up member 100042") == "looked up member 100042"


def test_sensitive_outputs_are_reported_for_screenshot_masking(redactor: Redactor) -> None:
    """Masking happens before the PNG is encoded, so raw pixels of regulated data never
    reach disk - redaction at capture, not post-processing."""
    names = redactor.sensitive_output_names(capability_with(Sensitivity.INTERNAL))
    assert names == ["savingsBalance"]


def test_very_short_values_are_not_registered(redactor: Redactor) -> None:
    """Masking a two-character string would replace fragments of unrelated words and
    destroy the log while protecting nothing."""
    redactor.register("code", "AB")
    assert redactor.text("ABSOLUTELY fine") == "ABSOLUTELY fine"


# ------------------------------------------------------------------ structures


def test_nested_structures_are_redacted_throughout(redactor: Redactor) -> None:
    redactor.register("memberId", "100042")
    payload = {
        "step": "s3",
        "detail": {"member": "100042", "notes": ["ssn 123-45-6789", "ok"]},
        "count": 3,
    }
    out = redactor.value(payload)

    blob = json.dumps(out)
    assert "100042" not in blob
    assert "123-45-6789" not in blob
    assert out["count"] == 3, "non-string values must pass through untouched"
    assert "step" in out, "dict keys are field names and must not be mangled"


def test_disabling_redaction_is_explicit_and_total() -> None:
    plain = Redactor(RedactionPolicy(enabled=False), salt="s")
    assert plain.text("ssn 123-45-6789") == "ssn 123-45-6789"


# ------------------------------------------------------------------ correlation


def test_fingerprints_correlate_without_revealing(redactor: Redactor) -> None:
    """Two log lines can be shown to refer to the same member without either line
    containing the member's identifier."""
    one = redactor.fingerprint("100042")
    two = redactor.fingerprint("100042")
    other = redactor.fingerprint("100043")

    assert one == two
    assert one != other
    assert "100042" not in one


def test_fingerprints_do_not_join_across_runs() -> None:
    """Per-run salt: evidence from separate runs cannot be joined to rebuild a member's
    history from logs that individually reveal nothing."""
    assert Redactor(salt="run-a").fingerprint("100042") != Redactor(salt="run-b").fingerprint(
        "100042"
    )
