"""Turning screen text into typed values.

The caller asked for a balance, not for the string a particular tenant happened to
render. `"$4,182.55"`, `"4,182.55 USD"` and `"4182.55"` are the same number, and a
capability that returned three different strings for them would push that normalisation
onto every agent that calls it.

Parsing failures are their own error class (`OUTPUT_EXTRACTION_FAILED`) rather than a
generic failure: reaching the right screen and being unable to read the value is a
different problem from never getting there, and it points at a different fix.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

_CURRENCY_STRIP = re.compile(r"[^\d.\-]")
_INT = re.compile(r"-?\d[\d,]*")
_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y", "%b %d, %Y", "%d %b %Y")


class ExtractionError(ValueError):
    """The value was on screen but could not be read as the declared type."""


def parse(raw: str, parse_as: str, *, field: str = "value") -> Any:
    text = (raw or "").strip()
    if not text:
        raise ExtractionError(f"{field}: nothing to read")

    if parse_as == "text":
        return text

    if parse_as == "currency":
        return _currency(text, field)

    if parse_as == "integer":
        match = _INT.search(text)
        if not match:
            raise ExtractionError(f"{field}: no integer in {text!r}")
        return int(match.group(0).replace(",", ""))

    if parse_as == "number":
        return float(_currency(text, field))

    if parse_as == "date":
        return _date(text, field)

    raise ExtractionError(f"{field}: unknown parse type {parse_as!r}")


def _currency(text: str, field: str) -> Decimal:
    """Returns Decimal, never float.

    Binary floating point cannot represent 4182.55 exactly, and a system that reports
    balances to a bank should not introduce rounding error in the reporting.
    """
    cleaned = _CURRENCY_STRIP.sub("", text.replace(",", ""))
    if not cleaned or cleaned in ("-", "."):
        raise ExtractionError(f"{field}: no amount in {text!r}")
    negative = "(" in text and ")" in text  # accounting notation for a negative
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ExtractionError(f"{field}: cannot read {text!r} as currency") from exc
    return -value if negative and value > 0 else value


def _date(text: str, field: str) -> date:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ExtractionError(f"{field}: cannot read {text!r} as a date")


def jsonable(value: Any) -> Any:
    """Outputs cross a process boundary to the calling agent, so they have to serialise."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value
