"""Fail the build if a regulated value or a credential reached a file it should not.

Redaction is only worth as much as its weakest egress, and there are a lot of them here:
artifacts, event logs, run manifests, failure dumps, intervention records, discovery
transcripts. Any one of them is a leak. Checking that by eye on every change does not scale
and does not stay true, so it is a gate instead - run in CI on every push, and again at the
end of `build_evidence.py` so freshly generated evidence cannot be committed dirty.

The scope is the interesting part, and it is deliberately not "grep the repo for the
balance". Three different rules, because three different things are being promised:

1. **Regulated values must not survive into anything the system persists.** The fixture
   balance stands in for a member's balance. It is allowed to exist in `targetapp/`, which
   *is* the fake core banking system, and in the tests that assert on it. It is not allowed
   in a capability artifact, an event log, a manifest, a failure dump, or an LLM
   transcript, because none of those are where an answer is delivered.

   The one deliberate exception is `console.txt`: a capability whose declared output is a
   savings balance necessarily prints that balance to the operator who asked for it.
   Redaction is about *incidental retention*, not about refusing to answer the question.

2. **Credentials must not appear anywhere at all.** Never an output, never a fixture worth
   keeping. Matched by shape rather than by value, since the key that matters is the one
   nobody thought to add to a list.

3. **The demo password must not appear in evidence at all**, console included - unlike the
   balance, it is never something a caller asked for.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Everything the system writes and we commit. This is the surface redaction has to hold
#: across; `targetapp/`, `src/` and `tests/` are excluded on purpose - see rule 1.
GENERATED = ("capabilities", "evidence/demo")

#: Rendered by the app, so it appears in a dozen legitimate places. Only its presence in a
#: *persisted* file is a finding.
BALANCE = ("4,182.55", "4182.55")

#: Never an answer to anything.
CREDENTIAL_VALUES = ("demo-pass",)

#: Credential *shapes*, checked across every tracked file. Catches the key that is not on
#: any list, which is the entire point of checking by shape.
CREDENTIAL_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "an Anthropic API key"),
    (re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"), "an OpenAI project API key"),
    (re.compile(r"sk-[A-Za-z0-9]{32,}"), "an API key"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "a GitHub token"),
]

#: The operator's terminal, where the declared output is delivered. See rule 1.
OUTPUT_CHANNEL = "console.txt"

#: Three files that must contain credential-shaped strings in order to do their job: this
#: one names every pattern it looks for, the packaging tests synthesise a fake key to
#: prove `.env` loading works, and the redactor tests assert that a key *does* get masked.
#: Scanning them is a guaranteed self-hit. Every value in them is invented.
EXEMPT = {
    "scripts/check_secrets.py",
    "tests/unit/test_packaging.py",
    "tests/unit/test_redactor.py",
}

TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".py", ".yaml", ".yml", ".html", ".cfg", ".toml"}


def tracked_files() -> list[Path]:
    """Every file git would ship. Falls back to a walk outside a repo, so the check still
    works from an extracted archive."""
    try:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        paths = [REPO / line for line in out.splitlines() if line]
        if paths:
            return paths
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return [p for p in REPO.rglob("*") if p.is_file() and ".git" not in p.parts]


def scan() -> list[str]:
    findings: list[str] = []
    scanned = generated = 0

    for path in tracked_files():
        rel = path.relative_to(REPO).as_posix()
        if rel in EXEMPT or path.suffix not in TEXT_SUFFIXES or not path.exists():
            continue

        text = path.read_text(encoding="utf-8", errors="ignore")
        scanned += 1

        for pattern, what in CREDENTIAL_PATTERNS:
            if pattern.search(text):
                findings.append(f"{rel}: looks like {what}")

        if not rel.startswith(GENERATED):
            continue
        generated += 1

        for value in CREDENTIAL_VALUES:
            if value in text:
                findings.append(f"{rel}: contains the demo credential {value!r}")
        if path.name == OUTPUT_CHANNEL:
            continue
        for value in BALANCE:
            if value in text:
                findings.append(f"{rel}: contains a member's balance {value!r}")

    print(
        f"scanned {scanned} tracked text files for credential shapes, "
        f"{generated} of them generated artifacts checked for regulated values"
    )
    return findings


def main() -> int:
    findings = scan()
    if findings:
        print("\nLEAK - refusing to pass:")
        for finding in sorted(set(findings)):
            print(f"  {finding}")
        return 1
    print("clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
