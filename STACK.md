# Stack, Explained — and Requirement Traceability

**Locked decision: all-Python 3.13. One language, one toolchain.**

Companion to [PLAN.md](PLAN.md) (*how to build it, day by day*) and [UNDERSTAND.md](UNDERSTAND.md) (*what the project is and why*). This document is *what it's made of, what each piece actually does, how every requirement in the brief maps to a file and a command, and how to defend all of it.*

---

## 1. The organising idea

Everything hangs off one decision: **the artifact schema is a Pydantic model, and that single declaration is the source of truth for four different consumers.**

```
src/cua/schema/capability.py        ← one Pydantic model
            │
            │  .model_json_schema()
            │
   ┌────────┼────────────┬─────────────────┬──────────────────┐
   ▼        ▼            ▼                 ▼                  ▼
static     runtime    LLM tool         agent-facing      human review
typing   validation   definitions      capability         (JSON diff
(mypy)   (on load)    (discovery)      contract           in git)
```

In TypeScript you'd get static typing from `tsc` and then need Zod *on top* for the other three, because TS types don't exist at runtime. Pydantic gives all four from one class. That's the whole language argument, and it's why the schema — which the brief calls "a focal point of the evaluation" — is the cheapest part of this build rather than the most expensive.

---

## 2. Every component, and what it's for

### The ones that carry the design

**Pydantic v2** — *the artifact schema, the result contract, the policy config, the tenant overlays.*
Defines `Capability`, `Step`, `Locator`, `ReplayResult`, `Policy`. Three things it buys that matter:
- `Field(json_schema_extra={"sensitivity": "pii"})` puts the redaction policy **inside the schema**. Safety becomes data the redactor reads, not `if` statements scattered across the codebase.
- Discriminated unions give the five-variant `ReplayResult` (`success | business_outcome | escalated | blocked | failed`) with validation for free.
- `model_json_schema()` is the agent-facing contract. A calling AI agent reads it to know what to pass and what it gets back.

**Playwright — async API, Chromium.** *Everything that touches the browser.*
Confined to exactly one file (`surface/web_surface.py`) so the rest of the system never knows a browser exists. What it gives you:
- `locator.aria_snapshot()` → the accessibility tree as compact YAML. **This is the perception layer.** The model reads roles and accessible names, never HTML. That's what makes the design honest about "no clean DOM."
- `get_by_role()` / `get_by_label()` → semantic locators, tiers 1–3 of the ladder.
- **Auto-waiting** — Playwright retries actionability checks internally, which removes most flake before you write a line of wait logic.
- `route()` → network-layer allowlist (safety layer 2).
- `tracing` → `trace.zip`, openable in Playwright's viewer. Your rich failure signal for requirement 3.5.
- `new_cdp_session()` → Chrome DevTools Protocol, for the screencast handoff.
- `screenshot(mask=[...])` → draws boxes over sensitive fields **before the PNG is encoded**, so raw PII never touches disk.

> ⚠️ **Async API, not sync.** On Day 5 automation must park mid-run awaiting a human while FastAPI keeps serving the operator console — same event loop. Sync Playwright raises inside asyncio. Choosing this wrong on Day 1 costs you a rewrite on Day 5.

**openai `>=3.6`** — *the discovery loop only. Never touched during replay.*
`chat.completions.create` with function tools and `parallel_tool_calls=False` — a browser has one cursor, so parallel tool calls would interleave clicks on state the earlier click already invalidated. The model is `CUA_MODEL` (default `gpt-4o`); a long agentic loop rewards the strongest tool-use model a key can reach.
> The SDK is confined to `discovery/llm.py`. The agent builds **provider-neutral** `Message` / `ToolSpec` types and the client translates at the last moment — so swapping provider is one file, and an import-linter contract proves replay never loads it at all.

**import-linter** — *makes an architectural claim mechanically true.*
Three contracts in `setup.cfg`: replay may not import `openai` or `cua.discovery`; Playwright may not escape the surface adapter; the schema layer depends on nothing. Requirement 3.3 says replay must run "without invoking the LLM" — this **proves** it in CI instead of promising it in a README. Very few submissions will have this.

### The ones that make it runnable

**Python 3.13** (pinned in `.python-version`) — every dependency ships 3.14 wheels, so 3.13 is the conservative pin. Pinning matters more than the number: a grader on 3.11 must get your tree.

**uv** — env, dependency resolution, lockfile, *and the interpreter itself*. `uv.lock` committed → the grader gets byte-identical deps. Chosen over Poetry (slower, doesn't manage interpreters) and bare pip/venv (no lockfile).

**FastAPI + Uvicorn** — the operator console API and WebSocket. Async-native (must share the event loop with Playwright) and Pydantic-native, so your `Intervention` model *is* the API response model — no serializer layer. `/docs` gives a free browsable API surface to demo.

**Typer** — the CLI. Type hints become argument parsing, validation, and `--help`. Eight subcommands with argparse would be ~150 lines of boilerplate.

**structlog** — writes `events.jsonl` directly. Requirement 3.5 asks for a *structured* log; stdlib `logging` with f-strings would mean hand-rolling a JSON formatter.

**Jinja2** — templates for the fake target app. Lets you write *deliberately awful* markup (framesets, nested tables, churning IDs) readably.

**tenacity** — bounded retry with backoff. Recovery must be capped and counted or "deterministic" quietly becomes "eventually consistent."

**PyYAML** — `policy.yaml`. Guardrails must be reviewable by a compliance person, not buried in Python.

### Testing and quality

**pytest + pytest-asyncio** — unit tests, browser integration tests, and the eval matrix (three markers).
**ruff** — lint + format in one tool. Replaces black + isort + flake8.
**mypy --strict** — Pydantic gives runtime validation; mypy gives compile-time. "Reasonably typed" is a literal rubric row.
**GitHub Actions** — one workflow: `ruff` → `mypy` → `lint-imports` → `pytest -m unit` → `cua eval` (which needs no API key). ~30 lines. Two things it buys beyond hygiene: the **import-linter contract proving replay never touches the LLM runs publicly on every push**, and a green badge on the README tells a reviewer the eval matrix actually passes before they clone anything.

### The operator console

**Vanilla JS + HTML, served by FastAPI. No npm, no build step, no framework.**
One page: an `<img>` for the screencast, a context panel, four buttons, one WebSocket. ~150 lines. A framework would be ceremony; a bundler is one more thing that breaks on the grader's machine. If asked: *"the interesting complexity here is the artifact schema, not the build."*

### Deliberately absent

No database, no queue, no Docker, no cloud. Artifacts are JSON files in git — which is *what makes them reviewable and diffable*, the exact property requirement 3.2 asks for. The brief says explicitly it does **not** reward building scaling infrastructure. State this in REPORT.md as a decision, not an omission, and name where the seams would become services (§5, Q6).

---

## 3. Requirement → implementation traceability

Your interview cheat sheet. Every sub-requirement in the brief, the component that satisfies it, the command that proves it.

### 3.1 Goal-driven agent loop

| Sub-requirement | Component | Proof |
|---|---|---|
| Accept goal + target | `cli.py` (Typer) | `cua discover --goal "..." --target http://localhost:4000/tenants/meridian` |
| LLM observe → decide → act loop | `discovery/agent.py` — `tool_runner`, `disable_parallel_tool_use` | `evidence/runs/disc_*/transcript.json` |
| Stopping conditions (max steps, timeout, dead-end) | step cap · wall clock · **observation-fingerprint no-progress detector** | `tests/unit/test_stopping_conditions.py` |
| Actually interacts with a real UI | `surface/web_surface.py` | `evidence/runs/disc_*/steps/*.png` |
| Works with no clean DOM | `surface/snapshot.py` — `aria_snapshot()` → role/name/`row_context`. **No CSS ever reaches the model.** | Target app has zero test IDs and IDs that regenerate every render |

### 3.2 Structured artifact

| Sub-requirement | Component | Proof |
|---|---|---|
| Ordered steps | `schema/capability.py::Step` | `capabilities/member.savings-balance@1.0.0.json` |
| Element identification + robustness reasoning | `locator/` — 7-tier ranked ladder, uniqueness required, cross-validation | REPORT.md §Determinism; `test_locator_ladder.py` |
| Typed input parameters | `Capability.inputs` — pattern, sensitivity, example | `cua catalog --show member.savings-balance` |
| Typed outputs + shape | `Capability.outputs` — type + extraction binding + parse rule | `ReplayResult.outputs` |
| Checkpoint / success condition | `success_condition` + per-step `post_assert` | Replay fails loudly if unmet |
| Versioned | `schema_version` + semver `version` + `provenance` | Two versions committed |
| Reviewable by human **and** agent | per-step `intent` prose; JSON Schema for agents | `cua catalog` renders both |

### 3.3 Deterministic replay

| Sub-requirement | Component | Proof |
|---|---|---|
| **Replay without the LLM** | `replay/` | ⭐ import-linter contract **+** `test_replay_never_loads_the_llm_sdk` asserting `"openai" not in sys.modules` after a real run |
| Stable element targeting | `locator/resolve.py` — walk the ladder, require uniqueness | Tier telemetry logged per step |
| Verify the checkpoint | `replay/assertions.py` | `CHECKPOINT_FAILED` row in the matrix |
| Return declared outputs | `replay/executor.py` | `cua replay ... --json` |
| **Expected business outcomes** | `known_outcomes` — declarative detectors | `cua replay --input memberId=999999` → `MEMBER_NOT_FOUND` |
| **Recoverable conditions** | `recoveries` + `replay/recovery.py`, bounded | `?fault=slow`, `?fault=interstitial`, `?fault=expire` |
| **Hard failures** | `replay/classifier.py` → step, expected, observed, trace.zip | `?fault=500` |
| Structured result | `schema/result.py` — 5-variant discriminated union | Every matrix row |

### 3.4 Safety & policy guardrails

| Sub-requirement | Component | Proof |
|---|---|---|
| Configurable allowlist (domains/routes **and** action types) | `policy.yaml` + `policy/engine.py` (**action layer**) + Playwright `route()` (**network layer**) | `test_policy_matrix.py`; blocked-origin event in evidence |
| Agent must not act outside it | Enforced inside every tool's `run()` — the model gets `blocked by policy:` as a tool result and cannot route around it | `cua discover --target https://evil.example` → refuses |
| Safe vs. risky classification | `policy/risk.py` — read_only / reversible_write / irreversible_write | `test_risk_classification.py` |
| Handle risky conservatively | **Fail closed.** Discovery → escalate. Replay → needs `status==approved` **AND** `--approve` **AND** declared step risk | `cua replay --capability account.open-subaccount` → `blocked`; `--approve` → `success` |
| Never persist secrets / raw PII | `policy/redactor.py` at every egress: logs, artifacts, **screenshots** (masked pre-encode), **and prompts** (model sees `«param:memberId»`, never the value) | `test_redactor.py`; CI greps `evidence/` |

### 3.5 Evidence / observability

| Sub-requirement | Component | Proof |
|---|---|---|
| Structured log of what **and why** | `evidence/logger.py` → `events.jsonl`; `why` is a required tool arg → becomes `step.intent` | `evidence/runs/*/events.jsonl` |
| Richer signal on failure | `trace.zip` + failure screenshot + full aria snapshot + last 3 observations | `evidence/runs/rep_*_failed/failure/` |

### 3.6 Human-in-the-loop escalation & handoff

| Sub-requirement | Component | Proof |
|---|---|---|
| Detect stuck | 6 triggers: `request_human`, no-progress, `RECOVERY_EXHAUSTED`, `UNKNOWN_DIALOG`, `RISKY_STEP_NEEDS_APPROVAL`, `LOCATOR_AMBIGUOUS` | `cua eval --scenario unknown-dialog` |
| Route with context | `control/intervention.py` — capability, goal, step + intent, reason, URL, screenshot, aria snapshot | `GET /interventions/{id}` |
| **Take control of the same live session** | `control/session.py` — exclusive lease + epoch; `BrowserContext` never torn down. CDP screencast → WS → `<img>`; input forwarded via `Input.dispatchMouseEvent` | Console on `:4100` |
| Hand control back | `POST /interventions/{id}/resume` → lease returns, epoch bumps, automation **re-orients** before continuing | Run completes after handoff |
| Preserve context + evidence | Same `events.jsonl`, same run dir, `actor` field | `evidence/runs/rep_*_escalated/` |
| **Record what the human did** | `add_init_script` capture listener + `expose_binding` → `{role, accessible_name, frame_path}` | `human_actions[]` in the manifest |
| Know who is in control | `SessionController.owner` + `epoch`; out-of-turn action raises `ControlLeaseViolation` | `test_control_lease.py` |

### 3.7 Heterogeneity & scale

| Sub-requirement | Component | Proof |
|---|---|---|
| Surface abstraction / the seam | `surface/base.py` ABC. Playwright appears in exactly one file — **enforced by import-linter** | REPORT.md mapping table: legacy web, Windows UIA (`uiautomation`), 3270, Citrix |
| Multi-tenant reuse | `schema/tenant.py` + `tenants/*.json` overlays composed at replay; artifact never mutated | ⭐ **Record on Meridian, replay on Lakeside** |
| Detect & manage drift | Screen fingerprint diff + **locator-tier telemetry** + `cua verify --tenants all` | Conformance report in evidence |

---

## 4. Why it won't collapse mid-week

The real failure mode of a 7-day build isn't a bad library — it's discovering on Day 5 that a Day 2 choice forbids what Day 5 needs. There are four such traps. All four are prevented on Day 0.

| Trap | When it bites | The Day-0 decision |
|---|---|---|
| **Sync vs. async Playwright** | Day 5 — the console must serve requests while automation is parked | **`playwright.async_api` from the first line.** Non-negotiable. |
| **A CSS selector leaking into the model or the artifact** | Day 4 — replay becomes brittle and the thesis collapses | `Surface` returns `Observation`/`ElementNode`. **No field can hold a CSS selector.** The type system forbids the mistake. |
| **Redaction bolted on at the end** | Day 7 — retrofitting across logs, screenshots, artifacts, prompts is a lost day | Build `Redactor` on **Day 2**, before any evidence exists; make it the only writer to disk. |
| **OpenAI SDK 0.x examples** | Day 3 — confusing failures | Pin `openai>=3.6`; use only `@beta_tool` + `tool_runner`. |

### Phase gates

Don't start phase N+1 until phase N's gate is green. If a gate fails twice, take the fallback — **every fallback still satisfies the requirement.**

| # | Phase | Gate | Fallback |
|---|---|---|---|
| 0 | Setup | `uv run cua --help` lists all subcommands | — |
| 1 | Target app + Surface | `uv run cua observe --url .../search` prints a clean role/name tree **from inside a nested frame** | Drop the frameset, keep table hostility |
| 2 | Schema + locators + policy + redaction | `uv run pytest -m unit` green; a locator re-resolves after reload with regenerated IDs | Drop tiers 5–7, keep 1–4 |
| 3 | Discovery loop | `uv run cua discover` emits a schema-valid artifact | Hand-author the artifact, ship `--mock`, **document it** — the brief permits a cleanly mocked boundary |
| 4 | Replay + taxonomy | `uv run cua eval` all green | 8 fault scenarios → 5 |
| 5 | Escalation | Unknown-dialog replay pauses → console shows context → human acts → resume → completes | **Tier A** (headed browser + resume button) ships first, always |
| 6 | Stretch | `uv run cua agent "balance for member 100042?"` returns a real number | Cut entirely. Zero rubric impact. |
| 7 | Write-up | A stranger clones, runs, and understands in 10 minutes | **Do not code on Day 7.** |

### The "no live services" path (the brief asks for this; most people skip it)

```bash
uv run cua discover --mock    # replays a recorded LLM transcript. No API key.
uv run cua replay ...         # never needed a key
uv run cua eval               # full matrix, zero API calls
```
Record the fixture on Day 3 *while you have real transcripts in hand*.

---

## 5. Defense sheet

| If asked… | Answer |
|---|---|
| **"Why Python?"** | Pydantic collapses type definition, runtime validation, and JSON Schema emission into one declaration — TypeScript needs types *plus* Zod, because TS types are erased at runtime. And the desktop extension path in 3.7 has maintained Python libraries (`uiautomation`, `pywinauto`) where Node's UIA bindings are abandoned. Cost accepted: Playwright features land in Node first. |
| **"Why no TypeScript for the console?"** | It's 150 lines. A second language, an npm toolchain, and a codegen step to type one file wouldn't earn their keep — and appropriate simplicity is an explicit scoring line. Thirty-minute upgrade if it ever grows. |
| **"Why no database?"** | Artifacts are versioned documents that belong in git next to the code that runs them — that *is* the reviewability requirement. A DB adds migrations and removes `git diff` on capability changes. In production the registry becomes a service; `CapabilityRegistry` is already that seam. |
| **"Why Playwright over Selenium?"** | Auto-waiting, `get_by_role`, `aria_snapshot()`, CDP access. Selenium has none of them, so you hand-roll the wait strategy — the number-one source of replay flake. |
| **"Why not browser-use / Stagehand / Playwright MCP?"** ⭐ | I evaluated all three. They're the popular AI-browser layer right now, and every one of them is the wrong shape for this problem. `browser-use` *is* the agent loop and `Stagehand` *is* the perception layer — adopting either outsources the exact things being designed here (locator strategy, artifact schema, error taxonomy). More fundamentally, none of them emits a **reusable deterministic artifact**; they re-reason with a model on every single run. That's the naive approach this system exists to replace: too slow for a member on the phone, too expensive at thousands of calls a day, and non-deterministic in a regulated environment. I use raw Playwright precisely so the discovery→artifact→replay boundary is mine to design. |
| **"Why the raw OpenAI SDK and not LangChain / LangGraph?"** | I've shipped with LangGraph — it's the orchestration layer in SAGE, my compliance-agent project — so this isn't unfamiliarity. It's the wrong fit here. The discovery loop is ten tools and one state machine, and the policy check and control lease have to sit *inside* every tool call where a reviewer can see them, not behind a framework's callback system. LangGraph's `interrupt()` does give you human-in-the-loop, but it pauses *graph* state; my hard problem is pausing a **live browser session** and transferring its lease, which LangGraph has no opinion about. Adding it would mean a reviewer has to trust the framework's semantics instead of reading a twenty-line `SessionController`. |
| **"Why `gpt-4o`?"** | Long-horizon tool-use reliability and 1M context, so a 30-step run with screenshots never needs compaction mid-discovery. It's an env var. Discovery runs *once per capability* and replay never calls a model, so model cost amortizes to near zero. |
| **"How do I know replay truly has no LLM?"** | Two mechanisms. An import-linter contract forbids `cua.replay` from importing `openai`. And a test runs a full replay in a subprocess and asserts the module was never *loaded*. The second is the stronger claim. |
| **"What if OpenAI is down?"** | Replay is unaffected — that's the entire point of the architecture. Discovery is an offline authoring step, not a production path. |
| **"Where does this break at 2,000 app instances?"** | Not authoring — record per *product*, override per tenant, and ~2,000 instances collapse to ~20 products. Drift management breaks first: I'd need the nightly `verify` sweep, per-tenant conformance dashboards, an override-proposal workflow. Then session pooling and secret management — neither of which I built. |
| **"What's the weakest part?"** | Discovery quality. The compiler's step-pruning and auto-parameterization are heuristics; on a complex flow I'd expect to hand-edit the artifact before approving it. That's *why* `status: draft` exists and unattended replay is gated on `approved`. Next investment: a diff-and-review UI for artifacts. |

---

## 6. Day 0 — exact commands

```bash
cd /Users/yeshwanthbalaji/Documents/InterfaceAI_TakeHome

uv init --python 3.13 --name cua
uv add playwright pydantic pydantic-settings "openai>=3.6" \
       fastapi "uvicorn[standard]" typer structlog jinja2 tenacity pyyaml
uv add --dev pytest pytest-asyncio ruff mypy import-linter types-pyyaml
uv run playwright install chromium

git init && git add -A && git commit -m "chore: scaffold"
gh repo create <name> --public --source=. --remote=origin --push
```

Commit `.python-version` and `uv.lock`. Add `.env` to `.gitignore` **before** the first commit; ship `.env.example`.
