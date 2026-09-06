# Computer-Use Automation

[![ci](https://github.com/YeshwanthBalaji0412/computer-use-automation/actions/workflows/ci.yml/badge.svg)](https://github.com/YeshwanthBalaji0412/computer-use-automation/actions/workflows/ci.yml)

**An LLM learns a legacy banking UI once. A typed capability artifact replays it forever — with no model in the loop.**

Banks and credit unions run a long tail of internal applications with no API: core banking screens, servicing tools, admin consoles. The only way in is the way a teller does it — log in, type into a form, click, read the screen.

Pointing an LLM at that screen on every request fails in production on four counts: **cost** (re-reading a screen that hasn't changed in six years, thousands of times a day), **latency** (a member is on the phone), **non-determinism** (auditors dislike "it took a different path today"), and **risk** (a model that improvises can improvise on a screen that moves money).

So this system splits the problem in two:

> **The model discovers.** Once. Slow, expensive, smart.
> **Deterministic replay invokes it.** Forever. Fast, cheap, auditable, and with no model involved in any decision.

The design write-up is in **[REPORT.md](REPORT.md)**. A plain-language explanation of the problem is in **[UNDERSTAND.md](UNDERSTAND.md)**.

---

## Setup

Requires Python 3.13 (managed by [uv](https://docs.astral.sh/uv/)) and Chromium.

```bash
git clone https://github.com/YeshwanthBalaji0412/computer-use-automation.git
cd computer-use-automation
uv sync
uv run playwright install chromium
```

That's it. **No API key is needed for anything below except live discovery** — see [Running without live services](#running-without-live-services).

---

## Demo path

Two terminals. The first runs the fake bank; the second drives it.

```bash
# terminal 1 — the target application
uv run cua serve-app
```

### 1. Discover a flow — an LLM figures it out and a capability is compiled

```bash
uv run cua discover \
  --goal "Look up member 100042 and read their current savings balance" \
  --target http://localhost:4000/tenants/meridian/
```

Writes `capabilities/member.savings-balance@1.0.0.json` and a full run log under `evidence/runs/`.

> Needs `OPENAI_API_KEY`. To run the identical code path with no key, use `uv run cua discover --mock` — it replays the transcript in `evidence/fixtures/`, which is a **real `gpt-4o` run**, not a stand-in.

**Recording a flow that writes** needs a human present, and that is enforced rather than advised:

```bash
uv run cua discover --operator-port 4100 \
  --goal "Open a SAVINGS sub-account nicknamed HOLIDAY FUND for member 100042" \
  --target http://localhost:4000/tenants/meridian/
```

Policy escalates every irreversible action *during discovery* — the model is by definition
working out a UI it does not yet understand, which is the worst possible moment to let it
press **Confirm and Open Account**. The run parks, the console asks you to authorise that
one action, and on approval automation performs its own proposed step so the recording
keeps the model's stated intent. **Without `--operator-port` the answer is no** and the run
stops: an unattended recording session cannot open an account because nobody was watching.

### 2. Replay it deterministically — this is the production path

```bash
uv run cua replay --capability member.savings-balance --input memberId=100042
```

```
status     success
outputs    {"savingsBalance": "4182.55"}
```

### 3. Watch it handle everything that goes wrong

```bash
uv run cua replay --capability member.savings-balance --input memberId=999999
```
```
status     business_outcome
outcome    MEMBER_NOT_FOUND: No member exists with that identifier.
           (a legitimate answer, not a failure)
```

The whole taxonomy, in one command:

```bash
uv run cua eval
```
```
SCENARIO               EXPECTED                     ACTUAL
happy-path             success                      success              PASS
different-member       success                      success              PASS
not-found              MEMBER_NOT_FOUND             MEMBER_NOT_FOUND     PASS
permission-denied      PERMISSION_DENIED            PERMISSION_DENIED    PASS
invalid-input          failed                       invalid_input        PASS
known-interstitial     success                      success              PASS
session-expiry         success                      success              PASS
unknown-dialog         escalated                    unknown_dialog       PASS
app-error              app_error                    app_error            PASS
slow-load              success                      success              PASS
write-blocked          blocked                      blocked              PASS
write-duplicate        DUPLICATE_RECORD             DUPLICATE_RECORD     PASS
write-ambiguous        ambiguous_write_outcome      ambiguous_write_out  PASS

13/13 scenarios passed
```

Thirteen rows, **nine different answers**. A system that returned `failed` for the middle rows would pass a naive smoke test and be useless in production — the caller could not tell "this member does not exist" from "the automation is broken".

The last three rows are the write flow, and they are the ones I would open first. **`write-blocked`** is refused *before it touches the page* — a policy refusal is not a malfunction, so it is `blocked` rather than `failed`, and the caller's response is "get approval", not "retry harder". **`write-duplicate`** is a clean answer: the application refuses at the review screen before committing, which is exactly what makes a capability that must never retry a *step* safe for a caller to retry as a *whole*. And **`write-ambiguous`** is the one that matters most — see below.

Two other rows are worth reading twice. **`session-expiry` succeeds**, and the interesting part is *how*: the content frame swaps to a sign-in form while the top-level URL never changes, so anything watching the address bar sees a healthy run. Detection has to come from the screen. And recovery is not a retry — signing in again lands on the entry screen, not the one that failed — so the artifact declares `restart_from_step` and the run picks up from there. **`unknown-dialog` escalates** rather than recovering, because the application is asking an operator a question and guessing the answer is the one decision this system is not allowed to make.

### 4. Run something irreversible — two gates, and neither one is optional

`member.open-subaccount` opens a real sub-account. Replaying it takes **two independent
approvals**, and you can watch them fall away one at a time:

```bash
uv run cua replay --capability member.open-subaccount \
  --input memberId=100042 --input nickname="ROOF FUND"
```
```
status     blocked
rule       risk_gates.reversible_write
attempted  click - review the new sub-account
needs      capability.status == approved, --approve
```

Nothing was clicked. Policy is checked *before* the locator is resolved, so a refused step
never touches the application at all. Now a human reviews the recording:

```bash
uv run cua approve member.open-subaccount --reviewer ops@meridiancu.example
```
```
member.open-subaccount@1.0.0 approved by ops@meridiancu.example.
  No replay history yet - approving on inspection alone.
  This capability is irreversible_write. Replay will now run its risky steps,
  and still requires --approve on each run.
```

Replay again and the refusal is **still there**, but shorter — `needs --approve`. That is
the design: `risk_class` is a property of the action, `status` is how much this *recording*
is trusted, and a per-invocation `--approve` is this *caller* accepting the consequence.
Three different decisions, made by different people at different times.

```bash
uv run cua replay --capability member.open-subaccount \
  --input memberId=100042 --input nickname="ROOF FUND" --approve
```
```
status     success
outputs    {"confirmationNumber": "SA-655584"}
```

**Now run that exact command again.**

```
status     business_outcome
outcome    DUPLICATE_RECORD: A record with those details already exists; nothing was created.
           (a legitimate answer, not a failure)
```

That sentence — *nothing was created* — is what makes a non-idempotent capability safe for
a caller to retry. The application refuses at the review screen, one step before it
commits.

**And the case that is genuinely hard:**

```bash
uv run cua replay --capability member.open-subaccount \
  --input memberId=100042 --input nickname="KAYAK FUND" --approve --fault write-timeout
```
```
status     escalated
reason     ambiguous_write_outcome
at step    s11
```

The confirm returned a server error. **Whether the account was opened is unknown, and no
amount of reading the screen will settle it** — the screen *is* the error. So the run stops
and asks a human to check the system of record.

Now go and be that human. Ask for the same account again:

```bash
uv run cua replay --capability member.open-subaccount \
  --input memberId=100042 --input nickname="KAYAK FUND" --approve
```
```
status     business_outcome
outcome    DUPLICATE_RECORD: A record with those details already exists; nothing was created.
```

**It had been opened.** The write committed and only the acknowledgement was lost. Reporting
`APP_ERROR` — which is what the screen literally says, and what this system used to do —
would have invited a retry and given the member two sub-accounts. That is the only place
here where "I don't know" is the correct answer, and being able to say it is the whole
reason `idempotent` is a field rather than a comment.

### 5. Hand a stuck run to a human, mid-session

```bash
uv run cua replay --capability member.savings-balance \
  --input memberId=100042 --fault unknown-dialog --operator-port 4100
```

The run hits an undeclared dialog, **parks**, and prints an intervention URL. Open **`http://localhost:4100/`**: you get the live session streamed over CDP, the reason it stopped, what it already tried, and a takeover button.

Click **Take control**, dismiss the dialog on the live view, then **I cleared the obstacle**. The run resumes and finishes. It is the *same* browser session throughout — same cookies, same auth, same half-filled form.

### 6. Replay the same artifact at a different institution

```bash
uv run cua replay --capability member.savings-balance --input memberId=100042 --tenant lakeside
```

Lakeside runs the same vendor product, but its search button says **Find Member**, its ID field says **Member Number**, it *requires* a branch selection first, and it renders balances as a definition list instead of a table. The capability was recorded against Meridian and is replayed unmodified:

```
status     success
outputs    {"savingsBalance": "4182.55"}
drift      suspected - the screen's shape differs from the recording
locators   2 step(s) resolved at a lower tier than recorded
```

It didn't just work — it **said which two steps leaned on the overlay**. That is the per-tenant drift signal.

```bash
uv run cua verify --capability member.savings-balance
```
```
TENANT        STEPS  DEGRADED  UNRESOLVED  NOTES
lakeside          8         2           0  s4:t1->t3, s7:t4->t5
meridian          7         0           0  clean
```

### 7. See what the calling AI agent sees

```bash
uv run cua catalog --show member.savings-balance
```
```json
{
  "name": "member_savings_balance",
  "description": "Looks up a member by ID and retrieves their current savings balance...
    Returns: savingsBalance (currency). May instead return one of these expected outcomes,
    which are answers rather than errors: MEMBER_NOT_FOUND, PERMISSION_DENIED,
    VALIDATION_REJECTED, DUPLICATE_RECORD, APP_ERROR.
    Status: draft - not yet approved for unattended use.",
  "arguments_schema": {
    "type": "object",
    "properties": {
      "memberId": { "type": "string", "pattern": "^\\d{6}$", "examples": ["100042"] }
    },
    "required": ["memberId"],
    "additionalProperties": false
  }
}
```

This is the payoff of putting business outcomes in the schema. The tool description tells a
calling agent that `MEMBER_NOT_FOUND` is a possible **answer** *before it ever invokes* — so
it can plan around it instead of treating every non-success as an outage. The schema is
generated from the same Pydantic declaration that types the code and validates the artifact
on load, so the contract cannot drift from the implementation.

---

## Watching it work

```bash
uv run cua replay --capability member.savings-balance --input memberId=100042 --headed
```

A real browser window opens and drives itself. Worth doing once — then view source on the page and look at the `ctl00_ContentPlaceHolder1_frmSearch_ctl33_btnSearch` control IDs it is deliberately *not* using. They are regenerated on every render.

```bash
uv run cua observe --url http://localhost:4000/tenants/meridian/home
```

Prints exactly what the model sees: roles and accessible names, never HTML.

---

## Running without live services

Only `cua discover` calls a model. Everything else — replay, the eval matrix, escalation, cross-tenant, the whole test suite — runs offline.

| Command | API key |
|---|---|
| `cua serve-app`, `cua observe` | no |
| `cua replay`, `cua eval`, `cua verify` | **no, by design** |
| `cua catalog`, `cua approve` | no |
| `cua discover --mock` | no — replays a real recorded `gpt-4o` transcript |
| `cua discover` | yes |

Replay never needing a key is not a convenience, it is the thesis. It is enforced two ways: an `import-linter` contract forbids `cua.replay` from importing `openai`, and a test runs a real replay in a subprocess and asserts the module was **never loaded**.

For live discovery:

```bash
cp .env.example .env      # then set OPENAI_API_KEY
```

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | — | Live discovery only |
| `CUA_MODEL` | `gpt-4o` | Discovery model |
| `CUA_DEMO_USERNAME` / `CUA_DEMO_PASSWORD` | `operator` / `demo-pass-not-a-real-secret` | Fake credentials for the local app, resolved through `secret_ref` and never written into artifacts or logs |

Guardrails live in **[`src/cua/policy/policy.default.yaml`](src/cua/policy/policy.default.yaml)** — deliberately YAML, so a compliance reviewer can read what the agent may do without reading Python.

---

## Evidence

Committed, and reproducible with `uv run python scripts/build_evidence.py`:

| Path | What it shows |
|---|---|
| [`evidence/demo/member.savings-balance@1.0.0.json`](evidence/demo/) | The capability artifact |
| [`evidence/demo/discovery/`](evidence/demo/discovery/) | A discovery run: events, transcript, compiled artifact |
| [`evidence/demo/replay-success/`](evidence/demo/replay-success/) | The production path |
| [`evidence/demo/replay-not-found/`](evidence/demo/replay-not-found/) | A business outcome, not an error |
| [`evidence/demo/replay-app-error/`](evidence/demo/replay-app-error/) | A hard failure with debuggable context |
| [`evidence/demo/replay-escalated-handoff/`](evidence/demo/replay-escalated-handoff/) | A run parked, resolved by a human, resumed |
| [`evidence/demo/replay-lakeside/`](evidence/demo/replay-lakeside/) | The same artifact at another institution |
| [`evidence/demo/eval-matrix.txt`](evidence/demo/eval-matrix.txt) | All ten scenarios |

Every run directory contains `events.jsonl` (a structured log of what happened **and why**), `manifest.json`, a rendered `report.md`, `steps/*.png` (per-step screenshots, masked before encoding), a Playwright `trace.zip` you can open in `npx playwright show-trace`, and on failure an `aria.txt` showing what the system *perceived* — the difference between that and the screenshot is usually the bug.

Redaction is enforced by a gate, not by review: [`scripts/check_secrets.py`](scripts/check_secrets.py) runs in CI and again at the end of every evidence build. No credential appears in any committed file, in any form, and a member's balance appears in **nothing the system persists** — not the artifact, not the event log, not the manifest, not a failure dump, not the LLM transcript. The one place it does appear is `console.txt`, because a capability whose declared output is a savings balance has to print that balance to the operator who asked for it. Redaction is about incidental retention, not about refusing to answer the question.

---

## Development

```bash
uv run pytest -m unit           # ~4s, no browser
uv run pytest -m integration    # ~20min, real Chromium
uv run ruff check . && uv run mypy && uv run lint-imports
uv run python scripts/check_secrets.py
```

`lint-imports` enforces three architectural contracts: replay may not import the LLM SDK, Playwright is confined to the surface adapter, and the schema layer depends on nothing else.

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs all of the above on every push, plus the integration suite and the full eval matrix against a real Chromium. **It needs no secrets** — so the badge above is a claim a reviewer can verify from a fork: the eval matrix passes, and the contract proving replay never touches the LLM holds, before they clone anything.

---

## What this is built on

Python 3.13 · Playwright (async) · Pydantic v2 · FastAPI · Typer · OpenAI SDK. One language, one toolchain, no database, no queue, no Docker. The reasoning for each choice — and for the ones rejected — is in [REPORT.md](REPORT.md) and [STACK.md](STACK.md).

The target application in [`targetapp/`](targetapp/) is a deliberately hostile stand-in: framesets, nested layout tables, zero test IDs, and ASP.NET-style control IDs regenerated on every render. It is built rather than borrowed because no public demo site can produce "record not found", a permission denial, a session timeout, an undeclared modal and a 500 on demand — and those are the interesting cases.

No real credentials, no real PII, nothing automated against a third-party site.
