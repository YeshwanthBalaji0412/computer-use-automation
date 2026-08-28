# interface.ai Take-Home — Build Plan

**Project codename:** `handspan` (the layer that gives agents hands). Rename if you like — pick one and use it consistently.

**Thesis in one sentence:** *An LLM discovers how to drive a legacy bank UI once, we compile what actually worked into a typed, versioned, tenant-portable capability artifact, and a deterministic replay engine executes that artifact in production with a real error taxonomy, real guardrails, and a real human-handoff seam.*

Everything below serves that sentence. If a feature doesn't, cut it.

---

## 0. How to read this plan

| Tag | Meaning |
|---|---|
| **P0** | Must exist or the submission fails a rubric row. Non-negotiable. |
| **P1** | Strongly differentiating. Build if P0 is done. |
| **P2** | Stretch. Build at most two, only if P0+P1 land by Day 6. |

The grading rubric (Section 7 of the brief) is weighted in this order: **System design → Core loop correctness → Robustness/error handling → Human-in-the-loop → Generalization → Safety → Code quality → Communication.** Note that *breadth of features is explicitly not rewarded.* Your enemy is scope creep, not lack of features.

---

## 1. What "done" looks like

A grader clones your repo and runs four commands. Each one must work on a clean machine.

```bash
uv sync && uv run playwright install chromium
uv run cua serve-app                                    # starts the fake legacy bank console on :4000
uv run cua discover --goal "Look up member 100042 and read their savings balance" \
                    --target http://localhost:4000/tenants/meridian
uv run cua replay   --capability member.savings-balance@1.0.0 --input memberId=100042
uv run cua replay   --capability member.savings-balance@1.0.0 --input memberId=999999   # business outcome
```

...plus one command that needs no API key at all (`uv run cua discover --mock`), one that triggers escalation, and one that shows an AI agent invoking the capability by name.

**Deliverables checklist (exact paths — the brief says they read submissions side by side):**

- [ ] `/README.md` — setup, config, **how to run with no live services**, demo path
- [ ] `/REPORT.md` — exactly these H2s: `## Architecture`, `## Artifact schema`, `## Determinism & error handling`, `## Heterogeneity & multi-tenant`, `## Escalation & handoff`, `## Safety`, `## Cuts`
- [ ] `/evidence/` — one artifact + discovery run logs + replay run logs (success, business outcome, escalation) + a short screen recording (optional but high ROI)
- [ ] Public GitHub repo, no secrets committed, `.env.example` present
- [ ] Email `assignments@interface.ai` from `balaji.y@northeastern.edu` with the repo URL **on its own line**, no zip

---

## 2. Stack decision (and the defense)

### Recommendation: **All-Python. Python 3.13, single process, single toolchain.**

> Full component list, version pins, requirement traceability matrix, and defense sheet: **[STACK.md](STACK.md)** — that document is authoritative for stack questions.

| Layer | Choice | Why this and not the alternative |
|---|---|---|
| Language | **Python 3.13, everywhere** | Pydantic v2 is one source of truth that simultaneously (a) types the code, (b) validates artifacts at load, (c) emits JSON Schema for the agent-facing capability contract, and (d) emits the LLM tool schemas. One declaration, four consumers. TypeScript would need Zod *on top of* the type system to get (b), because TS types are erased at runtime. One language, one toolchain — `uv sync` and you're running. |
| Browser control | **Playwright, async API (Chromium)** | Auto-waiting (kills 80% of flake for free), `get_by_role()` accessibility-first locators, `locator.aria_snapshot()` for a stable perception layer, `tracing` for evidence, `route()` for network-level allowlisting, and `new_cdp_session()` for the human-handoff screencast. Selenium has none of the first three. **Async API is mandatory** — the operator console must serve HTTP/WS while automation is parked mid-run. |
| Schema | **Pydantic v2 → JSON Schema** | See above. One definition, four consumers. |
| LLM | **Anthropic SDK, `claude-opus-5`** (env-configurable) | Best tool-use reliability for long agentic loops; 1M context so a 40-step discovery run with screenshots never needs compaction. Adaptive thinking, `effort: "high"`. Model ID is an env var so a grader can flip to `claude-sonnet-5` for cost. |
| Target app | **A local, deliberately-hostile "legacy" bank console (FastAPI + Jinja2)** | See §3. |
| Persistence | **Files on disk** (`capabilities/*.json`, `evidence/runs/<id>/`) | The brief explicitly says building scaling infrastructure is *not* rewarded. A DB would be a negative signal — and artifacts are versioned documents that belong in git next to the code, which is the reviewability requirement. Say so in REPORT.md. |
| Process model | **One Python process**, Typer CLI, with an embedded FastAPI server for the operator console | Justify in REPORT.md: the control-transfer seam is an interface (`SessionController`), not a network boundary. Splitting into services adds ceremony and zero design insight at this scale. Note where the seam would become a service boundary in production (session broker + run workers). |

**Reject explicitly in the write-up (shows you considered them):**
- *A CUA/computer-use SDK with raw screenshot+coordinates only* — great for discovery, terrible for deterministic replay: pixel coordinates are the least stable locator that exists. I use screenshots as *model context*, never as the primary locator.
- *A record-and-replay browser extension (Selenium IDE style)* — records CSS/XPath, which is exactly the brittleness the brief is warning about.
- *All-TypeScript* — genuinely close, and Playwright's TS API is the reference implementation. Two things decided it against: Pydantic collapses typing + runtime validation + JSON Schema into one declaration where TS needs types *plus* Zod, and the desktop-surface extension path in §3.7 has maintained Python libraries (`uiautomation`, `pywinauto`) where Node's UIA bindings are abandoned. Accepted cost: Playwright features land in Node first.
- *Python core + a TypeScript operator console* — considered and rejected. Generating TS types from the Pydantic JSON Schema would stop the browser and server drifting, but the console is ~150 lines. A second language, an npm toolchain, and a codegen step is not worth buying type-safety for one file — and "appropriate simplicity" is an explicit scoring line. If the console grows, the upgrade is 30 minutes.

### One-line hedge for the interview
> "The architecture is language-neutral — `Surface`, the artifact, `SessionController`, and the policy chokepoint are all interfaces. Porting to TypeScript is a day's work: Pydantic→Zod, `model_json_schema()`→`zod-to-json-schema`. I chose Python for the schema story and the desktop extension path, and kept it to one language because the interesting complexity here is the artifact schema, not the build."

---

## 3. The target application (build this first — it's your leverage)

**Decision: build your own legacy-style app.** The brief explicitly blesses this ("a local sample app you build or mock, or — if you want to lean into the 'legacy/no-clean-DOM' reality — an intentionally hostile surface").

**Why this beats using a public demo site (e.g. saucedemo, the-internet, a shop):**
1. You need to *inject* the exceptional states the brief cares about — record-not-found, validation error, permission denial, surprise modal, session timeout, transient slowness, HTTP 500. No public site gives you all seven on demand.
2. You need a *second tenant variant* of the same vendor product for the multi-tenant story. Only possible if you own the app.
3. Zero ToS/rate-limit/PII risk (the brief calls this out twice).
4. The grader can run it offline, deterministically. Reproducibility is a rubric row (`easy to run`).

**Risk to neutralize:** "he built an easy target for himself." Neutralize by making it genuinely hostile and *documenting the hostility as a design decision*.

### App spec: **"CoreLink Servicing Console"** — a fake 2003-era credit-union back-office app

`targetapp/` — FastAPI + Jinja2, server-rendered HTML (no React, no client-side framework).

**Deliberate hostility (this list goes in the README — it's a selling point):**
- `<frameset>` with a nav frame and a content frame. Everything real happens in a nested frame. (Kills naive `page.click()`.)
- Layout by nested `<table>`. No CSS grid/flex. No semantic `<main>`/`<nav>`/`<section>` except where I intentionally add landmarks.
- **Zero `data-testid`.** Ever.
- ASP.NET-style churning IDs: `ctl00_ContentPlaceHolder1_grdMembers_ctl03_lnkView` — regenerated with a different `ctl` index on every render, so any recorded ID-based selector dies on run #2. This is the single most important hostile feature: it *forces* your locator strategy to be semantic.
- Classnames like `.x`, `.f7`, `.tbl2`.
- Server-side session cookie with a configurable idle timeout.
- Some buttons are `<a href="javascript:__doPostBack(...)">`, some are `<input type="submit">`, one is a `<td onclick="...">` that isn't focusable.

**But also — one deliberate concession:** every interactive control has a *visible human-readable label* (accessible name via text content, `alt`, `title`, or `<label for>`). Justify this: *this is exactly what real enterprise apps look like — no test IDs, garbage markup, but a human operator can read the screen. That readable surface is the only stable contract, which is why my locators are built on it.* This is the intellectual core of your submission.

### Flows to implement

**Flow A (primary, read-only):** Login → Member Search → enter Member ID → results table → click "View" on the row → Member Detail → read Savings balance.
**Flow B (secondary, write + risky):** From Member Detail → "Open Sub-Account" → 4-field form → Review screen → Confirm → Confirmation number.

**Demo it *both* ways — this matters.** The brief's own example goals include "open a new sub-account and reach the confirmation screen," so a Flow B that only ever prints `blocked` reads as *unfinished*, not as *safe*:
- `cua replay --capability account.open-subaccount --input ...` → `status:"blocked"`, `required_approval:["--approve","capability.status==approved"]`
- `cua replay ... --approve` → runs to the confirmation screen and returns the confirmation number as a typed output.

Same code path, two results. That demonstrates the guardrail *and* the write flow, and shows the gate is a deliberate policy decision rather than a missing feature.

**Idempotency (the brief puts it in the glossary — treat that as a hint).** Flow B is `idempotent: false`. Two consequences the replay engine must honour, or the field is decoration:
1. **Never auto-retry a non-idempotent step.** A timeout after clicking "Confirm" is ambiguous — the sub-account may or may not exist. Retrying could double-open it. Replay must escalate (`AMBIGUOUS_WRITE_OUTCOME`), never retry.
2. **Pre-flight duplicate check.** Declare an optional `preflight` on the capability — for Flow B, search whether a sub-account with that nickname already exists and return `DUPLICATE_RECORD` as a business outcome instead of creating a second one. Makes the capability safe for an agent to call twice.

### Fault injection (the payoff)

A query param or header toggles faults, so replay tests are deterministic:

| Trigger | Simulated condition | Expected system response |
|---|---|---|
| `memberId=999999` | Record not found | **Business outcome** `MEMBER_NOT_FOUND` |
| `memberId=ABC` | Server-side validation error banner | **Business outcome** `INVALID_INPUT` |
| `memberId=100777` | "You are not authorized to view this member" | **Business outcome** `PERMISSION_DENIED` |
| `?fault=slow` | 8s delay on detail page | **Recoverable** — wait/retry, then succeed |
| `?fault=interstitial` | Surprise "System maintenance Saturday" modal | **Recoverable** — dismiss known interstitial, continue |
| `?fault=expire` | Session cookie invalidated mid-flow → login page | **Recoverable once** (re-auth) then **escalate** |
| `?fault=500` | App error page | **Hard failure** with evidence |
| `?fault=unknown-dialog` | An undeclared modal | **Escalate to human** |

### Tenant variants (for the multi-tenant story — cheap because you own the app)

Same code, two mounts:
- `/tenants/meridian` — "Meridian Credit Union", button labeled **"Search"**, field labeled **"Member ID"**, balance shown in a table.
- `/tenants/lakeside` — "Lakeside Federal CU", button labeled **"Find Member"**, field labeled **"Member Number"**, an extra "Select branch" dropdown step, balance shown in a definition list.

Record on Meridian. Replay on Lakeside with a **tenant overlay** file. That single demo is worth more than any other stretch goal.

---

## 4. Architecture

```
                    ┌─────────────────────────────────────────────┐
   CLI / Catalog ──▶ │  Orchestrator                               │
                    │   discover() | replay() | serve() | verify() │
                    └───────┬─────────────────────┬───────────────┘
                            │                     │
              ┌─────────────▼──────┐   ┌──────────▼────────────┐
              │ Discovery Engine   │   │ Replay Engine         │
              │  agent loop (LLM)  │   │  NO LLM in decisions  │
              │  Recorder          │   │  StateClassifier      │
              │  Compiler ────────────▶│  RecoveryEngine       │
              └─────────┬──────────┘   └──────────┬────────────┘
                        │      Capability          │
                        │      Artifact (JSON)     │
        ┌───────────────┴──────────────────────────┴─────────────┐
        │        Policy Engine  ·  Redactor  ·  Evidence Log      │  ← every action crosses these
        └───────────────────────────┬─────────────────────────────┘
                                    │
                     ┌──────────────▼──────────────┐
                     │  SessionController (lease)  │  who holds control: automation | human
                     └──────────────┬──────────────┘
                                    │
                     ┌──────────────▼──────────────┐
                     │  Surface (interface)         │
                     │   observe() → Observation    │
                     │   act(Action) → ActResult    │
                     │   resolve(Locator) → Handle  │
                     ├──────────────────────────────┤
                     │  WebSurface (Playwright)     │  ← the one implemented today
                     │  [DesktopSurface — designed] │
                     └──────────────────────────────┘
```

### The four seams that make this design defensible

Call these out by name in REPORT.md. They are what "not painting yourself into a corner" means concretely.

1. **`Surface`** — the *only* place that knows about Playwright. Everything upstream deals in roles, accessible names, and semantic locators. Swapping in a `DesktopSurface` (Windows UIA / macOS AX) is a new implementation of one interface, not a rewrite. **This is the answer to "heterogeneity."**
2. **Capability artifact** — the only thing that crosses discovery→replay. Discovery could be replaced by a human recorder or a codegen tool and replay wouldn't notice.
3. **`SessionController`** — the only thing that knows who holds the control lease. Automation and human are peers behind it. **This is the answer to "control transfer."**
4. **`PolicyEngine` + `Redactor`** — a mandatory chokepoint every action and every byte of persisted evidence passes through. Not sprinkled `if` statements. **This is the answer to "safety."**

### Repo layout

```
/README.md  /REPORT.md  /PLAN.md  /STACK.md  /.env.example
/pyproject.toml  /uv.lock  /.python-version
/setup.cfg                   # [importlinter] contracts — replay must not import anthropic
/src/cua
  cli.py                     # Typer: discover | replay | serve | catalog | verify | eval
  surface/
    base.py                  # ABC: Surface, Observation, Action, ActResult, ElementNode
    web_surface.py           # the ONLY file that imports playwright
    snapshot.py              # aria_snapshot() → indexed ElementNode[] (frame-aware)
  locator/
    models.py                # Locator = ranked list[LocatorStrategy]
    generate.py              # element → candidate strategies (record time)
    resolve.py               # strategies → unique Playwright Locator (replay time)
  schema/
    capability.py            # Pydantic: Capability, Step, Locator, IOParam, KnownOutcome…
    result.py                # Pydantic: ReplayResult discriminated union (5 variants)
    policy.py  tenant.py
    emit_json_schema.py      # → schemas/*.json → agent contract + LLM tool defs
  discovery/
    agent.py                 # the observe→decide→act loop
    tools.py                 # @beta_tool definitions
    prompt.py                # versioned system prompt
    recorder.py              # captures what actually executed
    compiler.py              # trace → Capability artifact
    mock_llm.py              # fixture replay for --mock (no API key)
  replay/                    # ← import-linter forbids `anthropic` anywhere in here
    executor.py              # step machine
    classifier.py            # Observation → business_outcome | recoverable | hard
    recovery.py              # bounded retry, dismiss, re-auth (tenacity)
    assertions.py            # checkpoint evaluation
  policy/
    engine.py  redactor.py  risk.py  policy.default.yaml
  control/
    session.py               # SessionController: the exclusive lease + epoch
    intervention.py          # store + context capture
    server.py                # FastAPI + WebSocket operator API
  catalog/
    registry.py              # artifacts → Anthropic tool defs
    demo_agent.py            # an agent invoking a capability by name
  evidence/
    logger.py  artifacts.py  report.py
/web/operator/               # operator console: index.html + main.js (vanilla, no build step)
/targetapp/                  # hostile legacy console + tenant variants + fault injection
/schemas/                    # generated JSON Schema (committed — it's the contract)
/capabilities/               # saved artifacts (committed)
/tenants/                    # tenant overlay files (committed)
/evidence/                   # demo runs (committed)
/tests/                      # unit/ integration/ eval/
```

---

## 5. The perception layer — how the agent "sees" (P0, build Day 1)

**This is the decision that most differentiates a good submission from a mediocre one.** Get it right.

The brief: *"Bias toward an approach that would still work when the surface has no clean DOM."* So: **never** put raw HTML or CSS selectors in front of the model, and never record a CSS selector in an artifact.

### The `Observation`

```ts
class RowContext(BaseModel):
    row_key: dict[str, str]        # {"Member ID": "100042", "Name": "J. RIVERA"}
    column_header: str

class ElementNode(BaseModel):
    ref: str                       # "e14" — stable only within this observation
    role: str                      # button | textbox | link | cell | heading ...
    name: str                      # accessible name, whitespace-normalized
    value: str | None = None
    states: list[str] = []         # disabled, checked, expanded, required, focused
    frame_path: list[str]          # ["main", "content"] — frameset survival
    section: str | None = None     # nearest landmark/heading — the scoping anchor
    row_context: RowContext | None = None   # for table cells → locator tier 4
    bbox: BBox                     # screenshots + last-resort coordinate fallback

class Observation(BaseModel):
    observation_id: str
    url: str                       # canonicalized: /member/100042 → /member/:id
    title: str
    frame_tree: list[str]
    elements: list[ElementNode]    # interactive + text-bearing, capped & prioritized
    aria_yaml: str                 # locator.aria_snapshot() — compact model context
    screenshot_path: Path | None = None      # redacted, masked
    fingerprint: str               # hash of role+name skeleton → drift + no-progress detection
```

**Build it from:** `locator.ariaSnapshot()` for the compact YAML the model reads, plus a DOM walk *inside the Surface only* to attach `bbox`, `framePath`, `rowContext`, and `section`. The DOM walk is an implementation detail of `WebSurface` — it never escapes the seam. A `DesktopSurface` would produce the same `ElementNode[]` from the OS accessibility tree.

**Token discipline:** cap at ~120 elements, prioritize (interactive > headings > table cells in the viewport > body text), and hand the model the aria YAML + a numbered element index, not both in full. Include the screenshot every N steps or when the YAML is ambiguous, not every step.

### The locator ladder (record many, resolve in order)

At record time, for the element the model acted on, **generate every strategy that uniquely resolves** and store them ranked. At replay time, walk the ladder and take the first that resolves to exactly one node.

| Tier | Strategy | Playwright realization | Why it's ranked here |
|---|---|---|---|
| 1 | Role + exact accessible name, scoped to section | `frame.getByRole('button',{name:'Search',exact:true})` | Semantic, human-visible, survives markup rewrites, works on desktop AX too |
| 2 | Role + normalized/regex name (case/whitespace/punct-insensitive) | `getByRole('button',{name:/find\s*member/i})` | Absorbs the most common per-tenant label drift |
| 3 | Label association | `getByLabel('Member ID')` | Legacy forms usually keep `<label for>` even when everything else rots |
| 4 | **Table semantics** | `getByRole('row').filter({hasText:'100042'}).getByRole('cell').nth(colIndexOf('Balance'))` | The killer feature for legacy table-layout apps. Row identified by *data*, column by *header text* — both stable, neither positional |
| 5 | Text anchor + proximity | "first `textbox` after text `Member ID:`" | For controls with no label and no name |
| 6 | Structural path (role-path, not CSS) | `frame > table[2] > row[3] > cell[1]` | Recorded but demoted; used only to *report* drift, never silently trusted |
| 7 | Coordinates (bbox) | `page.mouse.click(x,y)` | Last resort, gated behind config, required for canvas/Citrix/desktop cases. Present so the abstraction isn't a lie. |

**Three rules that make this rigorous:**
1. **Uniqueness is required.** A strategy that matches 0 or ≥2 nodes is skipped, not guessed at. Ambiguity → next tier → eventually `LOCATOR_AMBIGUOUS` hard failure.
2. **Cross-validation.** When ≥2 strategies resolve, assert they point at the same node. If they disagree, that's a drift signal — log it, prefer the higher tier, mark the run `drift_suspected`.
3. **Tier telemetry.** Every replay records which tier won per step. Steps resolving at tier 1 in recording but tier 4 in production are the early-warning signal for UI drift, per tenant. Aggregate into a conformance report. **This is your "secondarily, any UI drift" answer.**

> Interview line: *"I don't record selectors. I record an identity — role, accessible name, and the data context it sits in — and I re-derive a selector at replay time. That's why the same artifact runs on a tenant whose button says 'Find Member' instead of 'Search'."*

---

## 6. The capability artifact (P0 — the single most-graded object)

Design principle: **the artifact is a contract, not a macro.** A calling AI agent must be able to answer "what does this do, what do I pass, what do I get, what could go wrong" from the artifact alone, without reading code.

### Skeleton

```jsonc
{
  "schemaVersion": "1.0.0",
  "id": "member.savings-balance",
  "version": "1.2.0",                      // semver: patch=locator fix, minor=new optional input/output, major=contract break
  "displayName": "Read member savings balance",
  "description": "Looks up a member by ID in the CoreLink servicing console and returns their current savings balance and account status.",

  "target": {
    "surface": "web",                       // web | legacy-web | desktop  ← the heterogeneity hook
    "product": { "vendor": "corelink", "app": "servicing-console", "versionRange": ">=8.2 <9" },
    "entry": { "urlPattern": "{{baseUrl}}/search", "requiresAuth": true, "authProfile": "corelink.operator" }
  },

  "status": "approved",                     // draft | approved | deprecated  ← gates unattended replay
  "riskClass": "read_only",                 // read_only | reversible_write | irreversible_write
  "idempotent": true,

  "inputs": [
    { "name":"memberId", "type":"string", "required":true,
      "pattern":"^[0-9]{6}$", "description":"6-digit member number",
      "sensitivity":"internal", "example":"100042" }
  ],

  "outputs": [
    { "name":"savingsBalance", "type":"currency", "required":true,
      "sensitivity":"pii", "description":"Current available savings balance",
      "extraction": { "locator": {...}, "parse": "currency", "unit":"USD" } },
    { "name":"accountStatus", "type":"enum", "values":["ACTIVE","DORMANT","FROZEN"], "required":true, ... }
  ],

  "preconditions": [
    { "kind":"url_matches", "pattern":"{{baseUrl}}/**" },
    { "kind":"element_present", "locator": {...}, "describe":"Member Search form is visible" }
  ],

  "steps": [ /* see below */ ],

  "successCondition": {
    "all": [
      { "kind":"element_present", "locator":{ /* heading role=heading name=/member detail/i */ } },
      { "kind":"text_matches", "locator":{ /* member id field */ }, "pattern":"^{{inputs.memberId}}$" },
      { "kind":"output_extracted", "output":"savingsBalance" }
    ]
  },

  "knownOutcomes": [
    { "code":"MEMBER_NOT_FOUND", "terminal":true, "severity":"info",
      "detect":{ "any":[ {"kind":"text_present","pattern":"(?i)no (member|records) found"},
                         {"kind":"element_present","locator":{"role":"alert","name":"/not found/i"}} ] },
      "message":"No member exists with that ID.",
      "returns": { "found": false } },
    { "code":"PERMISSION_DENIED", "terminal":true, "severity":"warn",
      "detect":{ "any":[ {"kind":"text_present","pattern":"(?i)not authorized"} ] },
      "message":"Operator lacks permission to view this member." },
    { "code":"INVALID_INPUT", "terminal":true, "severity":"info",
      "detect":{"kind":"element_present","locator":{"role":"alert","name":"/invalid member id/i"}} }
  ],

  "recoveries": [
    { "id":"dismiss-maintenance-notice", "maxAttempts":1,
      "detect":{"kind":"element_present","locator":{"role":"dialog","name":"/maintenance/i"}},
      "action":{"type":"click","locator":{"role":"button","name":"/close|ok|dismiss/i"}} },
    { "id":"reauth-on-session-expiry", "maxAttempts":1,
      "detect":{"kind":"url_matches","pattern":"**/login*"},
      "action":{"type":"run_capability","capability":"auth.login@1"},
      "thenRestartFromStep":"s1" },
    { "id":"transient-load", "maxAttempts":3, "backoffMs":[500,1500,4000],
      "detect":{"kind":"step_timeout"}, "action":{"type":"wait_and_retry"} }
  ],

  "policy": {
    "allowedOrigins":["{{baseUrl}}"],
    "allowedActions":["navigate","click","fill","select","press","extract","assert","wait"],
    "maxSteps":24, "maxDurationMs":90000, "screenshotOnEveryStep":false
  },

  "tenantBindings": { "$ref": "tenants/*.json" },   // resolved at replay, never embedded

  "provenance": {
    "discoveredBy":"llm", "model":"claude-opus-5", "promptVersion":"discovery/v3",
    "discoveryRunId":"disc_01J...", "recordedAt":"2026-08-27T…Z",
    "recordedAgainst":{"tenant":"meridian","appFingerprint":"sha256:…"},
    "reviewedBy":"balaji.y@northeastern.edu", "reviewedAt":"…"
  },

  "stability": { "replays":14, "successes":13, "lastVerifiedAt":"…",
                 "locatorTierHistogram":{"1":38,"2":4,"4":6}, "flakinessScore":0.07 }
}
```

### Step

```jsonc
{
  "id": "s3",
  "intent": "Submit the member search",              // human-readable WHY — for reviewers
  "action": { "type": "click" },                     // navigate|click|fill|select|press|extract|assert|wait|scroll
  "target": {                                         // the ranked locator ladder
    "describe": "the Search button in the Member Search form",
    "strategies": [
      { "tier":1, "kind":"role_name", "role":"button", "name":"Search", "exact":true, "scope":{"kind":"section","name":"Member Search"} },
      { "tier":2, "kind":"role_name", "role":"button", "namePattern":"(?i)^(search|find member|lookup)$" },
      { "tier":5, "kind":"text_anchor", "anchor":"Member ID", "relation":"next_control", "role":"button" },
      { "tier":7, "kind":"bbox", "x":312, "y":188, "w":64, "h":22 }
    ],
    "framePath": ["mainFrame","contentFrame"]
  },
  "value": null,                                      // or {"literal":"..."} | {"param":"memberId"} | {"secretRef":"corelink.password"}
  "risk": "safe",                                     // safe | risky
  "waitFor": [{ "kind":"network_idle", "timeoutMs":10000 },
              { "kind":"element_present", "locator":{...}, "timeoutMs":10000 }],
  "preAssert":  [{ "kind":"element_present", "locator":{...}, "describe":"search form present" }],
  "postAssert": [{ "kind":"any_of", "of":[ {"kind":"element_present","locator":{"role":"table","name":"/results/i"}},
                                           {"kind":"known_outcome"} ] }],
  "onFailure": "classify",                            // classify | retry | skip | escalate | fail
  "timeoutMs": 15000, "retries": 2
}
```

### Twelve schema decisions to defend (write these in REPORT.md)

1. **`intent` on every step.** A reviewer approving a capability that touches member money needs to read prose, not JSON. Cheap; huge for the "reviewable" requirement.
2. **`target.strategies` is a ranked array, not a string.** Robustness is a property of the *artifact*, not of the replay engine's cleverness. It's inspectable and diffable.
3. **`preAssert` / `postAssert` on every step, not just at the end.** The brief's glossary defines "checkpoint" as *"a condition you assert to confirm you actually reached the state you expected, rather than assuming the click worked."* Per-step assertion is how you get a *debuggable* failure ("step s3 expected the results table, observed the login page") instead of "run failed."
4. **`knownOutcomes` is declarative data, not code.** Business outcomes are part of the *contract* the calling agent reads. `MEMBER_NOT_FOUND` shows up in the capability's docs, so the agent knows to handle it. This is the brief's "most common design mistake" — solved at the schema level, not with try/catch.
5. **`recoveries` are declarative too, with `maxAttempts`.** Prevents the classic "retry forever" bug and makes recovery behavior reviewable.
6. **Sensitivity on every input and output.** Drives redaction automatically instead of relying on a regex catching everything. A field declared `pii` is masked in logs, masked in screenshots, and never sent to the LLM.
7. **`secretRef`, never literal secrets.** Values resolve from env/secret provider at execution time. Structurally impossible to commit a password in an artifact.
8. **`riskClass` + `status` are separate.** Risk is about the action; status is about trust. `irreversible_write` + `draft` = never runs unattended. Two orthogonal gates.
9. **`policy` is embedded *and* intersected with global policy at runtime.** The artifact can only ever *narrow* what the global policy allows, never widen it. Attacker-model reasoning: a tampered artifact can't grant itself new permissions.
10. **`tenantBindings` by `$ref`, not embedded.** The artifact describes the *product*; tenants are a separate composition layer. That's the whole multi-tenant argument in one field.
11. **URLs canonicalized to patterns** (`/member/:id`, `{{baseUrl}}`). No tenant hostnames or record IDs baked into a reusable artifact.
12. **`stability` block.** Makes "approve for unattended replay" an evidence-based decision. Costs ~10 lines; buys the confidence/approval stretch goal almost for free.
13. **`idempotent` is load-bearing, not decorative.** It gates retry behaviour: a non-idempotent step that times out mid-write is *ambiguous*, so replay escalates as `AMBIGUOUS_WRITE_OUTCOME` rather than retrying and risking a double-write. Paired with an optional `preflight` duplicate check that returns `DUPLICATE_RECORD` as a business outcome. A calling agent can therefore safely retry the *capability* even though it must never retry the *step*.

### Result contract (equally graded — a discriminated union)

```ts
class Success(BaseModel):
    status: Literal["success"] = "success"
    outputs: dict[str, Any]; steps: list[StepTrace]; evidence: EvidenceRef; duration_ms: int

class BusinessOutcome(BaseModel):            # NOT an error. Does not raise.
    status: Literal["business_outcome"] = "business_outcome"
    outcome: Outcome                          # code, message, data
    steps: list[StepTrace]; evidence: EvidenceRef

class Escalated(BaseModel):
    status: Literal["escalated"] = "escalated"
    intervention: InterventionRef             # id, reason, step_id, resolved_by, human_actions
    resumed_result: "ReplayResult | None" = None

class Blocked(BaseModel):
    status: Literal["blocked"] = "blocked"
    policy: PolicyViolation                   # rule, attempted_action, required_approval

class Failed(BaseModel):
    status: Literal["failed"] = "failed"
    error: ReplayError                        # class, step_id, step_intent, expected,
                                              # observed, locator_tiers_tried, evidence

ReplayResult = Annotated[
    Success | BusinessOutcome | Escalated | Blocked | Failed,
    Field(discriminator="status"),
]
```

Five statuses, not two. **`business_outcome` is not an error and does not throw.** Say that sentence out loud in the interview.

---

## 7. Discovery loop (P0 — Day 3)

### Tools exposed to the model (`@beta_tool`, all `strict=True`)

| Tool | Args | Notes |
|---|---|---|
| `observe` | `{ includeScreenshot?: boolean }` | Returns aria YAML + element index. The model must call this before acting after any state change. |
| `navigate` | `{ url }` | Policy-checked against allowlist. |
| `click` | `{ ref, why }` | `why` is required — it becomes `step.intent`. Forcing the model to state intent measurably improves step quality *and* gives you free documentation. |
| `fill` | `{ ref, text, why, isParameter?, parameterName? }` | Model declares when a value is a parameter, not a constant. |
| `select` / `press` | `{ ref, value/key, why }` | |
| `extract` | `{ ref, outputName, type, why }` | Declares an artifact output. |
| `assert_state` | `{ describe, refs[] }` | Model declares a checkpoint it believes identifies this screen. |
| `note_outcome` | `{ code, describe, detectRefs[] }` | Model flags an exceptional screen it hit (e.g. it fat-fingered an ID and saw "not found") → seeds `knownOutcomes`. **Deliberately run one discovery pass that hits a not-found screen so this fires.** |
| `request_human` | `{ reason, whatIveTried }` | Escalation from *discovery*, not just replay. |
| `finish` | `{ summary, capabilityName, description }` | Terminates the loop. |

### Loop mechanics

```python
runner = client.beta.messages.tool_runner(
    model=os.getenv("CUA_MODEL", "claude-opus-5"),
    max_tokens=16000,
    thinking={"type": "adaptive"},
    output_config={"effort": "high"},
    tool_choice={"type": "auto", "disable_parallel_tool_use": True},  # ← actions MUST be sequential
    system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
    tools=[
        observe,
        navigate,
        click,
        fill,
        select,
        press,
        extract,
        assert_state,
        note_outcome,
        request_human,
        finish,
    ],
    messages=[{"role": "user", "content": goal_prompt}],
)
final = runner.until_done()
```

Key details worth calling out:
- **`disable_parallel_tool_use: true`.** A browser has one cursor. Parallel tool calls would interleave clicks on stale state. This is a one-line fix for a bug most submissions will ship.
- **Prompt caching breakpoint** after tools+system: the system prompt and tool schemas are byte-identical across every turn of a 30-step run. This is a real cost lever and shows API fluency.
- **Every tool `run()` goes through**: `SessionController.assertOwner('automation')` → `PolicyEngine.check(action)` → `Redactor` → `Surface.act()` → `Recorder.record()` → `EvidenceLog.append()`. Guardrails are enforced at the tool boundary, so *the model cannot route around them*, and a hostile/confused model gets a `tool_result` saying "blocked by policy: ..." rather than an unhandled exception.

### Stopping conditions (all P0)
`maxSteps` (default 30) · wall-clock timeout (default 5 min) · **no-progress detector** (observation `fingerprint` unchanged for 3 consecutive acts → dead end → escalate) · policy violation → block · model calls `request_human` · model calls `finish`.

### Recorder vs. Compiler — the important distinction

- **Recorder** logs what *executed*: the action, the resolved element's full candidate strategy list, the observation before and after, and whether the postcondition held. It records reality, not the model's narration.
- **Compiler** (post-run, deterministic, no LLM) turns the recording into a `Capability`:
  1. Prune dead ends — actions that produced no state change, back-navigations, failed attempts.
  2. Parameterize — any typed value matching a goal-provided value becomes `{param:...}` (the model's `isParameter` flag is a hint, the value match is the check).
  3. Canonicalize URLs → `{{baseUrl}}` + `/member/:id` patterns.
  4. Synthesize `postAssert` for each step from the diff between pre/post observations (what appeared that wasn't there before).
  5. Merge `knownOutcomes` from `note_outcome` calls + a **global outcome library** (session expiry, 5xx, generic `role=alert`) that every artifact inherits.
  6. Attach `provenance` and validate against the Pydantic model before writing.

> Interview line: *"The artifact is compiled from what actually worked, not from the model's transcript. The transcript is evidence; the artifact is the contract. That decoupling is why replay doesn't inherit the model's mistakes."*

---

## 8. Replay engine + error taxonomy (P0 — Day 4, the highest-value day)

**No LLM. Prove it mechanically**, two ways — and mention both in REPORT.md:
1. An **`import-linter` contract** in `setup.cfg` forbidding `src/cua/replay/**` from importing `anthropic`. Runs in CI.
2. A test that runs a full replay in a subprocess and asserts `"anthropic" not in sys.modules`. Stronger than the lint rule — it proves the module was never even *loaded*, not merely that the source doesn't name it.

### Step machine

For each step: `resolveTenantOverlay` → `preAssert` → `PolicyEngine.check` → `resolve locator (ladder)` → `act` → `waitFor` → `observe` → **`StateClassifier.classify(observation)`** → `postAssert` → next.

### `StateClassifier` — the piece that separates the three failure classes

Runs on **every** observation, and it runs *before* `postAssert`, so an exceptional state is recognized as itself rather than as "assertion failed."

Evaluation order matters (declare it in REPORT.md):
1. **Artifact `knownOutcomes`** → `business_outcome`, terminal, return to caller with the declared code. Not an error.
2. **Artifact `recoveries`** → attempt recovery, bounded by `maxAttempts`, log it, re-observe, re-classify.
3. **Global outcome library** (session expiry, HTTP 5xx page, generic unhandled `role=alert`/`role=dialog`) → recoverable if known, else escalate.
4. **Nothing matched but `postAssert` failed** → hard failure with full context.

### The error taxonomy (put this exact table in REPORT.md)

| Class | Codes | Response | Result status |
|---|---|---|---|
| **Business outcome** — a legitimate answer | `MEMBER_NOT_FOUND`, `INVALID_INPUT`, `PERMISSION_DENIED`, `DUPLICATE_RECORD`, `ACCOUNT_CLOSED` | Stop cleanly, return the code + declared payload | `business_outcome` |
| **Recoverable** — transient or known-interstitial | `TRANSIENT_LOAD`, `KNOWN_INTERSTITIAL`, `STALE_ELEMENT`, `SESSION_EXPIRED` | Bounded retry / dismiss / re-auth-once, then continue; every attempt logged | `success` (with `recoveries[]` in the trace) |
| **Needs a human** — safe to pause, unsafe to guess | `UNKNOWN_DIALOG`, `AMBIGUOUS_STATE`, `RISKY_STEP_NEEDS_APPROVAL`, `RECOVERY_EXHAUSTED` | Freeze session, raise intervention with context | `escalated` |
| **Policy** | `ORIGIN_NOT_ALLOWED`, `ACTION_NOT_ALLOWED`, `RISK_GATE` | Refuse the action, do not continue | `blocked` |
| **Hard failure** | `LOCATOR_UNRESOLVED`, `LOCATOR_AMBIGUOUS`, `CHECKPOINT_FAILED`, `APP_ERROR`, `TIMEOUT_EXCEEDED`, `OUTPUT_EXTRACTION_FAILED` | Stop, dump screenshot + aria snapshot + trace, return debuggable error | `failed` |

**Determinism levers (list them explicitly — the brief asks "how determinism is achieved"):**
- Semantic locators re-derived at runtime (§5), never recorded selectors.
- Playwright auto-waiting + explicit `waitFor` conditions. **Zero `sleep()` anywhere in the codebase.** Grep for it in review; it's a smell a grader will look for.
- Uniqueness required at resolution; ambiguity is an error, never "take the first one."
- Fixed viewport, fixed locale/timezone, `reducedMotion:'reduce'`, animations disabled — removes an entire class of flake.
- Every step gated on a precondition and verified by a postcondition, so the run cannot silently drift off-path.
- Outputs parsed by declared type (`currency` → normalized minor units), so `"$1,234.56"` and `"1234.56 USD"` produce the same value.
- Recovery attempts are capped and counted, so "deterministic" doesn't degrade into "eventually consistent."

---

## 9. Safety & guardrails (P0 — Day 2)

**Model: two enforcement layers + a mandatory redaction boundary.**

### Layer 1 — action-level (`PolicyEngine.check(action, context)`)
Every action from *any* actor (LLM tool call, replay step, even human handoff navigation) passes through. Config in `policy.default.yaml`, overridable per capability (narrowing only):

```yaml
allowedOrigins: ["http://localhost:4000"]
allowedPathPatterns: ["/tenants/*/**"]
deniedPathPatterns: ["**/admin/**", "**/export/**"]
allowedActions: [navigate, click, fill, select, press, extract, assert, wait, scroll]
deniedActions: [download, upload, file_chooser, new_tab, execute_script]
maxStepsPerRun: 30
maxRunDurationMs: 300000
riskGates:
  irreversible_write: require_human_approval
  reversible_write:   allow_if_capability_approved
  read_only:          allow
riskySignals:                  # substring/regex on the control's accessible name
  - "(?i)\\b(delete|remove|close account|wire|transfer|submit payment|approve|disburse|void)\\b"
```

### Layer 2 — network-level (`context.route()`)
Playwright request interception aborts any request to a non-allowlisted origin. **Defense in depth:** if the app 302s to an external SSO or an injected script tries to beacon data out, the action-level check never sees it — the network layer does. Log every abort as a policy event.

> Interview line: *"Action-level allowlisting controls what the agent chooses to do. Network-level allowlisting controls what the page can do on the agent's behalf. Prompt injection in a legacy app's data fields is a real threat in this environment, and only the second layer stops it."*

### Risk classification & handling
Classify each action `safe | reversible_write | irreversible_write` from: HTTP method inferred from the form, the control's accessible name matched against `riskySignals`, and the artifact's declared `riskClass`.

**Chosen policy (and the justification):** *block-then-escalate*, not silently-flag.
- **Discovery:** an `irreversible_write` action **pauses and raises an intervention** — the human approves or performs it. Rationale: during discovery the model is by definition operating on a UI it doesn't understand yet; that's the worst possible moment to let it press "Confirm Wire Transfer." Bonus: this reuses the escalation path, so one mechanism covers two requirements.
- **Replay:** `irreversible_write` requires **all three** of `capability.status === "approved"`, an explicit `--allow-risky` / `approvals` token from the caller, and the artifact declaring the step's risk. Missing any → `status:"blocked"` with `requiredApproval` naming what's missing. Fail *closed*.
- Rejected alternative: "flag and continue." At a bank, an unreviewed irreversible write is a regulatory incident, not a log line. Say this.

### Redaction (the `Redactor` chokepoint)
Applied to **everything leaving memory**: evidence logs, artifacts, screenshots, *and LLM prompts*.

- **Declared-field redaction (primary):** anything with `sensitivity: pii|secret` in the artifact is replaced with `«redacted:savingsBalance»`. Deterministic and complete for known fields.
- **Pattern redaction (backstop):** SSN, card PANs (Luhn-checked to cut false positives), routing/account numbers, email, phone, DOB.
- **Screenshots:** `page.screenshot(mask=[<locators of sensitive fields>])` — Playwright renders solid boxes over them *before* the PNG is encoded, so the raw pixels never touch disk. Redaction at capture, not post-processing.
- **LLM boundary:** sensitive parameter *values* are never sent to the model. During discovery the model types `«param:memberId»` and the Surface substitutes the real value locally. **The model never sees regulated data.** This is a strong, quotable design point.
- **Correlation without leakage:** store `HMAC-SHA256(value, runSalt)` when you need to prove two fields matched without storing either.
- **Honest limits (state them in REPORT.md — graders reward this):** full-page screenshots can still capture PII in *undeclared* fields; pattern matching is best-effort; the redactor cannot un-ring the bell if a value already reached the model. Production would need field-level classification from the app catalog + a DLP scan in CI on the `evidence/` directory.

---

## 10. Escalation & human handoff (P0 — Day 5; most submissions fumble this)

### The control-transfer model

```python
Controller = Literal["automation", "human", "none"]


class SessionController:
    def __init__(self) -> None:
        self._owner: Controller = "automation"
        self._epoch = 0  # bumps on every transfer
        self._resumed: asyncio.Future[HandoffResult] | None = None

    def assert_owner(self, who: Controller, epoch: int) -> None:
        if self._owner != who:
            raise ControlLeaseViolation(held_by=self._owner, attempted_by=who)
        if epoch != self._epoch:  # an action that started before the transfer
            raise StaleEpochError(expected=self._epoch, got=epoch)

    async def cede(self, reason: str, ctx: InterventionContext) -> HandoffResult:
        self._owner, self._epoch = "human", self._epoch + 1
        self._resumed = asyncio.get_running_loop().create_future()
        return await self._resumed  # automation parks here — same BrowserContext stays alive

    def resume(self, result: HandoffResult) -> None:
        self._owner, self._epoch = "automation", self._epoch + 1
        self._resumed.set_result(result)
```

This is where the **async Playwright API becomes non-negotiable**: automation is suspended on an `await` inside the same event loop that FastAPI is using to serve the operator console. With the sync API this deadlocks.

Three properties that make this a real model rather than a boolean flag:
1. **Exclusive lease.** Automation *cannot* act while the human owns it — `Surface.act()` asserts the lease and throws. Not "we politely stop"; structurally prevented. This is the answer to "there must be a way to know who is in control."
2. **Epoch counter.** An in-flight automation action that was mid-await when control transferred can't land after the human took over. Guards the real race condition in this design.
3. **Same session, always.** The `BrowserContext` is never torn down. Cookies, auth, scroll position, half-filled forms, and the frame tree all survive the handoff — because it's literally the same page object.

### Detect → route → take control → hand back

**Detect.** Triggers: `request_human` from the model · no-progress fingerprint detector · `RECOVERY_EXHAUSTED` · `UNKNOWN_DIALOG` · `RISKY_STEP_NEEDS_APPROVAL` · `LOCATOR_AMBIGUOUS`.

**Route.** Write an `Intervention` record and log it:
```jsonc
{ "id":"int_01J…", "runId":"rep_01J…", "raisedAt":"…",
  "capability":"member.savings-balance@1.2.0", "goal":"…",
  "currentStep":{ "id":"s5", "intent":"Open the sub-account confirmation", "attempt":2 },
  "reason":"UNKNOWN_DIALOG", "explain":"A dialog titled 'Overdraft Protection Notice' appeared; it is not in this capability's declared recoveries.",
  "state":{ "url":"…/subaccount/review", "screenshot":"evidence/…/int_01J.png", "ariaSnapshot":"…" },
  "suggestedActions":["Dismiss the dialog and continue","Abort the run"],
  "status":"open", "takeoverUrl":"http://localhost:4100/interventions/int_01J…" }
```
In production this is a queue message / webhook to the ops console. Locally it's a file + a URL printed to stdout. **Same payload either way** — that's the seam.

**Take control (two-tier build — this is the de-risking move):**

- **Tier A (P0, build first, ~1 hour):** browser launches headed. Operator console at `:4100` lists open interventions with full context and screenshot, and has **Take control / Resume / Mark step done / Abort** buttons. The human drives the actual Chromium window. Automation is parked on a promise. Works, is real, ships Day 5 morning.
- **Tier B (P1, upgrade Day 5 afternoon):** real in-browser co-browse via CDP. `context.new_cdp_session(page)` → `Page.startScreencast` streams JPEG frames over a WebSocket to the operator page; operator mouse/keyboard events are forwarded back via `Input.dispatchMouseEvent` / `Input.dispatchKeyEvent`. Now the operator needs nothing but a browser tab, and it works headless/remote — which is the actual production shape. ~200 LOC. If it fights you for more than 3 hours, ship Tier A and describe Tier B in REPORT.md.

**Capture what the human did (P0 — the brief asks for it explicitly).** One mechanism covers both tiers: `context.add_init_script()` installs a capture-phase listener for `click`/`change`/`submit` that reports `{role, accessible_name, frame_path, ts}` (never values for sensitive fields) through an `expose_binding`, plus `page.on("framenavigated")`. Every human action lands in the same evidence log as automation actions, tagged `actor:"human"`.

**Hand back — and this detail matters:** on resume, automation does **not** blindly continue. It re-observes and evaluates a **re-sync assertion**:
- If the *current* step's `postAssert` now passes → the human completed the step → advance.
- Else if the current step's `preAssert` passes → the human recovered the state → retry the step.
- Else if the successCondition passes → the human finished the whole flow → return success (with `completedBy:"human"`).
- Else → the human left the session somewhere unexpected → raise a second intervention rather than guess.

> Interview line: *"Resuming is a re-orientation problem, not a resume-from-a-line-number problem. The human may have done more, less, or something different than we asked. So I re-observe and let the assertions tell me where I actually am."*

**Design-only extras for REPORT.md (say you cut them, and why):** operator identity/authz and audit trail, multiple concurrent interventions with routing/SLA, "record the human's fix as a proposed artifact patch for review" (the obvious flywheel — mention it, it shows product sense), and a full co-browsing console with cursor presence.

---

## 11. Heterogeneity & multi-tenant (mostly design, one real demo)

### Surfaces
The seam is `Surface`. An artifact contains no browser concepts — only roles, accessible names, data context, and frame/window paths.

| Surface | Perception | Action | What changes in the artifact |
|---|---|---|---|
| Modern web | aria snapshot + DOM enrichment | Playwright | nothing |
| **Legacy web** (framesets, tables, no test IDs) | same, but tier-4 table semantics and tier-5 text anchors do most of the work | Playwright, frame-aware | nothing — `framePath` already exists. **This is what I actually built against.** |
| **Desktop** (Win32/WPF) | UI Automation tree → same `ElementNode` shape (UIA has `ControlType`≈role and `Name`≈accessible name) | `pywinauto`/FlaUI/WinAppDriver | `target.surface:"desktop"`, `framePath`→`windowPath`. Tiers 1–3 and 5 map directly; tier 4 maps to UIA Grid/Table patterns; tier 6 becomes a UIA runtime-id path. |
| Terminal / green-screen | 3270 field map → `ElementNode` with `rowContext` = screen coordinates | tn3270 send-keys | Only tier 5 (text anchor) and tier 7 survive; the ladder degrades but doesn't break |
| Citrix / pixel-only | OCR + vision model | coordinates | Only tier 7. Honest answer: this is the degenerate case; flag it as low-confidence, require higher `stability` before approval. |

**Why role+name is the right universal primitive:** it's the one identity that exists in the DOM accessibility tree, in Windows UIA, in macOS AX, *and* in a human operator's head. Every other locator concept is surface-specific. Say this.

### Multi-tenant
**Model: `Capability` is authored against a *product*; `TenantBinding` is a separate composition layer. Resolution is `resolve(capability, tenantOverlay)` at replay time — the artifact on disk is never mutated.**

```jsonc
// tenants/lakeside.json
{ "tenantId":"lakeside-fcu",
  "product":{"vendor":"corelink","app":"servicing-console","version":"8.4"},
  "baseUrl":"http://localhost:4000/tenants/lakeside",
  "vars":{ "memberIdLabel":"Member Number" },
  "capabilityOverrides":{
    "member.savings-balance":{
      "versionRange":">=1.2 <2",
      "steps":{
        "s3":{ "target":{ "prependStrategies":[
                 {"tier":1,"kind":"role_name","role":"button","name":"Find Member","exact":true}]}},
        "s4":{ "insertBefore":[{ "id":"s3b","intent":"Lakeside requires branch selection",
                                 "action":{"type":"select"},
                                 "target":{"strategies":[{"tier":3,"kind":"label","label":"Branch"}]},
                                 "value":{"literal":"MAIN"} }]}
      }
    }}}
```

Override kinds, deliberately limited: `prependStrategies` (add a locator candidate, keep the base ones as fallback), `insertBefore`/`insertAfter` (extra tenant step), `skip`, `overrideValue`, `addKnownOutcome`. **Notably you cannot override `successCondition`** — a tenant may not redefine what success means, because that's the contract the calling agent relies on. That constraint is a good thing to be asked about.

**Drift detection & management:**
- **Fingerprint.** Every run hashes the role+name skeleton of each screen. Recorded fingerprint vs. observed fingerprint → `drift_suspected` flag.
- **Tier telemetry.** Tenant X resolving step s3 at tier 4 when the base recorded tier 1 = a labeled control changed. Aggregate per tenant × capability × step into a conformance report.
- **`verify` command.** `uv run cua verify --capability X --tenants all` does a read-only smoke pass (preconditions + locator resolution only, no writes) across tenant profiles. In production this runs nightly; a tenant that starts failing gets an override PR, not a re-record.
- **Escalation ladder for drift:** tier-degradation → alert; locator unresolved on one tenant → generate an override candidate (the LLM-assisted repair path, bounded to a single step, human-reviewed); unresolved across all tenants → the vendor shipped a new version, re-record the base and bump the major.

> Interview line: *"Record once per vendor product, override per tenant, never re-record per tenant. Roughly 100 tenants × 20 apps is 2,000 instances but only ~20-ish products — so the unit of authoring has to be the product, and the tenant layer has to be a thin, reviewable diff."*

---

## 12. Evidence & observability (P0 — cheap, do it Day 2 so everything else feeds it)

Per run: `evidence/runs/<runId>/`
```
manifest.json      # runId, kind (discovery|replay), capability@version, tenant, inputs (redacted), result, timings, cost
events.jsonl       # one JSON object per event: {ts, seq, actor, phase, stepId, type, payload}
steps/s01-pre.png  steps/s01-post.png    # redacted, masked
failure/           # on failure: screenshot + full aria snapshot + last 3 observations + trace.zip
transcript.json    # discovery only, redacted LLM transcript, decoupled from the artifact
report.md          # generated human-readable summary
```
- Structured `events.jsonl` is the "structured log of what the agent did **and why**" (`why` = `step.intent` / tool `why` arg).
- Playwright `context.tracing.start(screenshots=True, snapshots=True)` always on for replay; `trace.zip` is the rich failure signal. A grader can open it in Playwright's trace viewer — that's a strong impression for ~5 lines of code.
- Log token usage + estimated cost per discovery run. Shows you think about production economics.
- `actor` field distinguishes `automation` / `human` / `system` — the handoff audit trail falls out for free.

---

## 13. Testing & evals (P1, but the JD literally says *"you own the evals for what you build"*)

**Unit (fast, no browser):** locator strategy generation + ranking + uniqueness; redactor (incl. Luhn false-positive cases); policy engine allow/deny/risk-gate matrix; state classifier detector matching; artifact Pydantic validation + a schema-version migration test.

**Integration (real browser, local app, no LLM):** replay the checked-in artifact against every fault-injection scenario in §3 and assert the exact `ReplayResult.status` and code. This is the highest-signal test in the repo — it *is* your error-taxonomy proof.

**Discovery test without an API key:** record one real discovery run's LLM responses to a fixture file; `--mock` replays them through a `MockLLM` implementing the same interface. Gives you (a) a fast deterministic test of the recorder+compiler, and (b) the brief's "how to run without live services." **Do this — it's explicitly requested and most people skip it.**

**Eval harness (`uv run cua eval`)** — a scenario matrix printing a pass table:
```
SCENARIO                     EXPECTED             ACTUAL               PASS
happy-path                   success              success              ✓
not-found                    MEMBER_NOT_FOUND     MEMBER_NOT_FOUND     ✓
invalid-input                INVALID_INPUT        INVALID_INPUT        ✓
permission-denied            PERMISSION_DENIED    PERMISSION_DENIED    ✓
slow-load                    success (1 retry)    success (1 retry)    ✓
interstitial                 success (1 recover)  success (1 recover)  ✓
session-expiry               success (re-auth)    success (re-auth)    ✓
app-500                      failed/APP_ERROR     failed/APP_ERROR     ✓
unknown-dialog               escalated            escalated            ✓
risky-step-no-approval       blocked/RISK_GATE    blocked/RISK_GATE    ✓
risky-step-approved          success              success              ✓
duplicate-subaccount         DUPLICATE_RECORD     DUPLICATE_RECORD     ✓
write-timeout-ambiguous      escalated            escalated            ✓
cross-tenant (lakeside)      success              success              ✓
stability x10                >=9/10               10/10                ✓
```
Paste this table into README.md. It is the most persuasive artifact in the entire submission — it demonstrates every claim in one screen.

---

## 14. Stretch goals — pick exactly these two (P2)

1. **Agent-facing capability catalog, exposed as an MCP server** (Day 6 morning, ~2h + ~40 lines). `CapabilityRegistry` loads `capabilities/*.json` and emits Anthropic tool definitions (name from `id`, description from `description`, `input_schema` from Pydantic-emitted JSON Schema, and the `knownOutcomes` listed in the description so the calling agent knows what can come back). Then `uv run cua agent "What's the savings balance for member 100042?"` — a Claude agent that has *only* the catalog tools, picks the right capability, invokes it, gets typed outputs back from a deterministic replay, and answers. **This closes the brief's own through-line loop and is the best possible closing demo.** ~90 lines.

   **Wrap the same registry as an MCP server** (`capabilities` → MCP tools). ~40 extra lines over the registry you already have, and it means any MCP-compatible agent — Claude Desktop, Claude Code, a customer's own agent — can discover and invoke your capabilities by name with typed args. This is the current standard answer to "expose a catalog of callable capabilities," so it's the one *fashionable* choice in the whole build that's also the *correct* one. Screenshot it working from Claude Desktop and put that in `/evidence/`.
2. **Cross-tenant reuse** (Day 6 afternoon, ~2h). Already 80% built via the tenant variants and overlay resolver from §11. Just run it and capture the evidence.

**Explicitly decline the rest in REPORT.md's `## Cuts`,** with one line each: code generation (a page-object emitter is a nice-to-have that proves nothing new about the design), assisted LLM fallback (I designed the bounded single-step repair path but didn't build it — describe the policy envelope), and confidence scoring beyond the `stability` block.

---

## 15. Day-by-day schedule (7 days, ~6 focused hours/day)

Start each day by re-reading the deliverables checklist in §1. End each day with a green `uv run ruff check && uv run mypy src && uv run pytest` and a commit.

### Day 0 — Setup (2h)
- `git init`, public GitHub repo, MIT license, `.gitignore` (`.env`, `node_modules`, `evidence/scratch`), `.env.example`.
- `uv init --python 3.13`; add `playwright pydantic pydantic-settings anthropic fastapi uvicorn typer structlog jinja2 tenacity pyyaml`, dev `pytest pytest-asyncio ruff mypy import-linter`. Exact block in [STACK.md §6](STACK.md).
- **GitHub Actions workflow** (~30 lines): `ruff` → `mypy` → `lint-imports` → `pytest -m unit` → `cua eval`. Needs no API key, so it runs on every push. Put the badge in the README — a reviewer sees the eval matrix passing before they clone.
- Commit `PLAN.md`. **Commit often with real messages** — the commit history is read as evidence of process.
- Stub `src/cua/cli.py` with all six subcommands printing "not implemented." Vertical skeleton first.

### Day 1 — Target app + Surface (6h) 🔑
- **AM:** `targetapp/` — frameset, login, member search, results table, member detail, sub-account flow. Hostile markup per §3. Fault injection via query params. Two tenant mounts.
- **PM:** `Surface` interface + `WebSurface`. Aria snapshot → `list[ElementNode]` with `frame_path`, `section`, `row_context`, `bbox`. `uv run cua observe --url ...` that dumps an observation to stdout — you'll use this constantly for debugging.
- **Gate:** you can print a clean semantic observation of a page inside a nested frame.

### Day 2 — Schema + Locators + Policy + Evidence (6h) 🔑
- **AM:** All Pydantic models (`Capability`, `Step`, `Locator`, `ReplayResult`, `Policy`, `TenantBinding`). Locator `generate.py` + `resolve.py` with the full ladder + uniqueness + cross-validation. **Unit-test the ladder now** — everything downstream depends on it.
- **PM:** `PolicyEngine` (both layers), `Redactor`, `EvidenceLogger`. Unit tests.
- **Gate:** given a page and an element, you emit a ranked locator that re-resolves uniquely after a full page reload with regenerated `ctl00_...` IDs.

### Day 3 — Discovery loop (6h) 🔑
- Tool definitions, versioned system prompt, `toolRunner` loop, stopping conditions, no-progress detector.
- `Recorder` + `Compiler`. First real artifact written to `capabilities/`.
- Save the LLM fixture for `--mock` **while you're here** — you'll have real transcripts in hand.
- **Gate:** `uv run cua discover` produces a schema-valid artifact for Flow A. Expect prompt iteration; budget 2h of the 6 for it.

### Day 4 — Replay engine + error taxonomy (6h) 🔑🔑 *highest-value day*
- **AM:** step machine, assertions, output extraction/parsing, `ReplayResult`.
- **PM:** `StateClassifier`, `RecoveryEngine`, the full taxonomy. Wire every fault-injection scenario and get the eval table green.
- **Gate:** all §13 integration scenarios pass except escalation.

### Day 5 — Escalation & handoff (6h) 🔑
- **AM:** `SessionController` lease + epoch. `Intervention` store + context capture. Tier A operator console (FastAPI + one HTML page + take-control/resume/abort). Human-action capture via `add_init_script`. Re-sync-on-resume logic.
- **PM:** Tier B CDP screencast + input forwarding. **Hard stop at 3h** — if it's not working, keep Tier A and write Tier B up as design.
- **Gate:** an unknown-dialog replay pauses, appears in the console with context, a human dismisses the dialog, clicks resume, and the run completes with `completedBy` recorded.

### Day 6 — Stretch + hardening (6h)
- **AM:** capability catalog → Anthropic tool defs → `uv run cua agent` demo.
- **PM:** cross-tenant demo on Lakeside. Full eval matrix run. Fix whatever it exposes. Multi-run stability x10.

### Day 7 — Write-up, evidence, polish (6h) 🔑
- **AM:** `REPORT.md`. Use the exact seven H2 headings. 1–3 pages — **be ruthless**, this plan has more material than fits; pick the sharpest version of each argument. Steal the tables from §5, §8, §11.
- **PM:** `README.md` (setup, `.env`, `--mock` path, exact demo commands, eval table). Capture `/evidence/`: one discovery run, one success replay, one not-found replay, one escalated replay. Record a 3-minute screen capture (optional but it makes the handoff undeniable). Final read-through as a stranger. Push. Email.

### Cut lines if you fall behind (in this order)
1. Tier B screencast → Tier A only.
2. Flow B (sub-account write flow) → keep the `blocked` demo, drop the `--approve` success path. **Say in REPORT.md that the approved path is gated, not missing**, or it reads as unfinished.
3. Cross-tenant demo → tenant overlay resolver written + unit-tested, second tenant described only.
4. Capability catalog stretch.
5. Reduce fault scenarios from 8 to 5 (keep: not-found, validation, interstitial, unknown-dialog, 500).

**Never cut:** the artifact schema, the replay error taxonomy, the escalation lease, redaction, the write-up. Those are the top four rubric rows.

---

## 16. Interview defense — the questions you will be asked

Rehearse a 60-second answer to each. Do not read them; know them.

1. **"Why not just record CSS selectors like every record-and-replay tool?"** → Show `ctl00_ContentPlaceHolder1_grdMembers_ctl03_lnkView` regenerating. Semantic identity is what's stable; markup is an implementation detail. Also: role+name is the only locator concept that also exists on desktop.
2. **"Your locator resolves at tier 4 instead of tier 1. Is the run still valid?"** → Yes, but flagged. Uniqueness was still required, cross-validation still ran, and the tier degradation is logged as a drift signal. Correctness and confidence are separate axes.
3. **"How do you know 'no such member' isn't a bug in your automation?"** → It's declared in the artifact's `knownOutcomes` with an explicit detector, discovered during recording and reviewed by a human before approval. If it *weren't* declared, it'd be an `UNKNOWN_DIALOG`/checkpoint failure and would escalate — which is the correct behavior for something we haven't been taught to recognize.
4. **"What if the model hallucinates a step during discovery?"** → It can't reach the artifact. The Recorder records executed actions with verified postconditions; the Compiler prunes anything that didn't change state. And the artifact is reviewed before `status:"approved"`.
5. **"The human breaks the page during handoff. Then what?"** → Resume is a re-orientation, not a jump. Re-observe, evaluate postAssert → preAssert → successCondition in that order; if none hold, raise a second intervention rather than guess. Also: every human action is captured in the same evidence log.
6. **"How do you stop the agent doing something destructive?"** → Four gates: allowlist at the action layer, allowlist at the network layer, risk classification with fail-closed gating on irreversible writes, and a `draft→approved` status. And during discovery, risky actions escalate rather than execute — the model is least trustworthy exactly when it's exploring.
7. **"Where does this break at 2,000 app instances?"** → Authoring per product with tenant overlays holds. What breaks first is *drift management* — I'd need the nightly `verify` sweep, per-tenant conformance dashboards, and an override-proposal workflow. Also secret management and per-tenant session pooling, which I deliberately didn't build.
8. **"Why one process? Isn't that a toy?"** → For this scope, yes, deliberately. The production shape is a session broker owning browser contexts, stateless run workers, and an intervention queue — and my seams (`Surface`, `SessionController`, the `Intervention` payload) are already the interfaces those services would expose. Building the queue would have cost a day and taught the reviewer nothing.
9. **"What's the weakest part?"** → Have an honest answer ready. Suggested: *"Discovery quality is the least deterministic part — the compiler's step-pruning and auto-parameterization are heuristics, and on a genuinely complex flow I'd expect to hand-edit the artifact before approving it. That's why `status: draft` exists. I'd invest next in a diff-and-review UI for artifacts."* Volunteering a real weakness with a real mitigation reads as senior.
10. **"How would you eval this?"** → The scenario matrix in §13, plus per-capability replay stability over time, tier-degradation rate as a leading indicator of drift, escalation rate as the human-cost metric, and cost-per-successful-invocation. Tie it to the JD line about owning your evals.

---

## 17. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Day 1 target app balloons into a weekend project | Two flows, ugly HTML strings, no styling beyond `<table border=1>`. Timebox to 4h. Ugly is *on-theme*. |
| Discovery prompt won't converge | Budget 2h for prompt iteration on Day 3. Fallback: reduce Flow A to search→detail→read. If it still fails, hand-author the artifact, ship `--mock` discovery, and document it honestly — the brief explicitly permits mocking a boundary cleanly. |
| CDP screencast eats Day 5 | Hard 3h cap. Tier A first, always. |
| REPORT.md sprawls to 8 pages | It says 1–3. Write it, then cut 40%. Tables over prose. Long report reads as inability to prioritize. |
| Secrets leak into the repo | `.env` gitignored from commit #1; `.env.example` only; run `git log -p | grep -iE 'sk-ant|api[_-]?key'` before pushing. |
| Evidence directory contains something real | Only ever use fake member IDs (`100042`, `100777`, `999999`) and fake names. Never real PII. State this in the README. |
| You over-build and under-explain | The rubric's last row is Communication, but the first is System design — and both are about *articulation*. Reserve all of Day 7. Do not code on Day 7. |

---

## 18. First three commands

See **[STACK.md §6](STACK.md)** for the exact Day-0 command block (uv + Playwright + git).

**Four Day-0 decisions that prevent a Day-5 collapse** — do not deviate:
1. `playwright.async_api`, never `sync_api`. The operator console must serve requests while automation is parked.
2. `Surface` returns `Observation`/`ElementNode` — **no field can hold a CSS selector.** The type system forbids the mistake.
3. Build `Redactor` on Day 2, before any evidence exists, and make it the only writer to disk.
4. Pin `anthropic>=1.1,<2`. The SDK went 1.0 recently; most tutorials online are 0.x and fail confusingly.
