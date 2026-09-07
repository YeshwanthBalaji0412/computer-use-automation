"""Keeping regulated data out of anything that leaves memory.

Applied to **every** egress path: the structured event log, capability artifacts,
screenshots, run reports, and the prompts sent to the model. There is no path that
writes to disk or to the network without going through here.

Two mechanisms, in priority order, because they fail differently:

**Declared-field redaction (primary).** Anything the capability marks `pii` or `secret`
is masked by name. Precise and complete for everything the schema knows about, which is
every input and output of every capability. This is the mechanism that actually protects
regulated data, and it works because sensitivity is declared in the artifact rather than
guessed at run time.

**Pattern redaction (backstop).** Regexes for SSNs, card numbers, emails, phones, and
credential shapes. Catches data nobody declared - a member's SSN rendered on a screen we
did not model, or an API key surfacing in a stack trace. Best-effort by construction, and
the card matcher is Luhn-checked specifically so a 16-digit reference number is not
mistaken for a PAN.

The backstop is judged on false positives as much as on catches. Six-digit member ids and
confirmation numbers are deliberately left legible: they are internal identifiers rather
than PII, and a redactor that masks everything produces evidence nobody reads, which is a
security failure of a slower kind.

Known limits, stated because a guardrail whose gaps you cannot name is not a guardrail:

* A full-page screenshot can capture PII in fields nobody declared. Masking works from
  declared locators, so undeclared ones are not covered.
* Pattern matching cannot recognise an account number that looks like any other integer.
* Nothing here can un-send a value that already reached the model. That is why sensitive
  parameters are substituted locally by the surface and never appear in a prompt at all -
  redaction is the second line, not the first.

Production would add field-level classification from the application catalogue and a DLP
scan over the evidence directory in CI.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Any

from cua.schema.capability import Capability, Sensitivity
from cua.schema.policy import RedactionPolicy

MASK = "«redacted:{}»"


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for char in reversed(digits):
        value = ord(char) - 48
        if alt:
            value *= 2
            if value > 9:
                value -= 9
        total += value
        alt = not alt
    return total % 10 == 0


class _Pattern:
    def __init__(self, label: str, regex: str, *, luhn: bool = False) -> None:
        self.label = label
        self.regex = re.compile(regex)
        self.luhn = luhn

    def scrub(self, text: str) -> str:
        def repl(match: re.Match[str]) -> str:
            found = match.group(0)
            if self.luhn and not _luhn(re.sub(r"\D", "", found)):
                # Not a card number, just a long number. Leaving it alone matters:
                # a redactor that masks every 16-digit string makes logs unreadable
                # and trains people to ignore it.
                return found
            return MASK.format(self.label)

        return self.regex.sub(repl, text)


#: Ordered most-specific first, so a card number is not partially eaten by a looser rule.
PATTERNS: list[_Pattern] = [
    _Pattern("ssn", r"\b\d{3}-\d{2}-\d{4}\b"),
    _Pattern("card", r"\b(?:\d[ -]?){13,19}\b", luhn=True),
    _Pattern("email", r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    _Pattern("phone", r"\b(?:\+1[ -]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b"),
    _Pattern("dob", r"\b(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])/(?:19|20)\d{2}\b"),
    #: A long bare digit run with no other structure. 9+ so six-digit member ids and
    #: confirmation numbers stay legible - those are internal identifiers, not PII, and
    #: masking them would make every log useless for debugging.
    _Pattern("account", r"\b\d{9,17}\b"),
    #: Credential *shapes*. Secrets are supposed to reach the surface through
    #: `secret_ref` and never enter a log at all, and `scripts/check_secrets.py` gates
    #: the repository - but both of those protect paths we anticipated. A key that turns
    #: up in an error message, a stack trace, or a page's own text is the case nobody
    #: planned for, which is exactly what a backstop is for. Matched by shape, because
    #: the key that matters is the one not on any list.
    _Pattern("api_key", r"\bsk-(?:ant|proj)-[A-Za-z0-9_-]{16,}"),
    _Pattern("api_key", r"\bsk-[A-Za-z0-9]{32,}\b"),
    _Pattern("token", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    _Pattern("token", r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}={0,2}"),
    #: JWTs carry claims in a base64 payload anyone can decode.
    _Pattern("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
]


class Redactor:
    """The single writer-side chokepoint for sensitive data."""

    def __init__(
        self,
        policy: RedactionPolicy | None = None,
        *,
        salt: str | None = None,
    ) -> None:
        self._policy = policy or RedactionPolicy()
        #: Per-run salt. Correlation works within a run's evidence; the same value in a
        #: different run hashes differently, so logs cannot be joined across runs to
        #: rebuild a member's history.
        self._salt = salt or secrets.token_hex(16)
        self._declared: dict[str, str] = {}

    # ------------------------------------------------------------------ declared

    def register_capability(self, capability: Capability, values: dict[str, Any]) -> None:
        """Register this invocation's sensitive input values for masking by name."""
        for param in capability.inputs:
            if param.sensitivity in (Sensitivity.PII, Sensitivity.SECRET):
                value = values.get(param.name)
                if value:
                    self.register(param.name, str(value))

    def register(self, label: str, value: str) -> None:
        """Mask every future occurrence of `value`, labelled with `label`.

        Short values are ignored: masking a two-character string would replace fragments
        of unrelated words and destroy the log's usefulness without protecting anything.
        """
        if len(value) >= 3:
            self._declared[value] = MASK.format(label)

    def fingerprint(self, value: str) -> str:
        """A stable, non-reversible handle for a value.

        Lets two log lines be shown to refer to the same member without either line
        containing the member's identifier.
        """
        digest = hmac.new(self._salt.encode(), value.encode(), hashlib.sha256)
        return f"h:{digest.hexdigest()[:12]}"

    # ------------------------------------------------------------------ apply

    def text(self, value: str) -> str:
        if not self._policy.enabled or not value:
            return value

        out = value
        if self._policy.declared_fields:
            for raw, mask in self._declared.items():
                out = out.replace(raw, mask)
        if self._policy.patterns:
            for pattern in PATTERNS:
                out = pattern.scrub(out)
        return out

    def value(self, obj: Any) -> Any:
        """Recursively redact strings inside any JSON-shaped structure.

        Dict *keys* are left alone deliberately - they are field names, and mangling
        them would make the evidence unparseable while protecting nothing.
        """
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {k: self.value(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.value(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(self.value(v) for v in obj)
        return obj

    def sensitive_output_names(self, capability: Capability) -> list[str]:
        """Outputs whose on-screen locators should be masked before a screenshot is
        encoded, so raw pixels of regulated data never reach disk."""
        return [
            o.name
            for o in capability.outputs
            if o.sensitivity in (Sensitivity.PII, Sensitivity.SECRET)
        ]
