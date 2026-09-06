# Design write-up

## Architecture

Two engines either side of one artifact.

```
   goal ──▶ Discovery (LLM) ──▶ Recorder ──▶ Compiler ──▶ capability.json
                                                               │
   agent ─────────────────────────────────────────────────▶ Replay (no LLM)
                                                               │
                     ┌─────────────────────────────────────────┴──────────┐
                     │ PolicyEngine · Redactor · EvidenceLogger           │
                     ├────────────────────────────────────────────────────┤
                     │ SessionController — who holds the control lease    │
                     ├────────────────────────────────────────────────────┤
                     │ Surface (ABC)  →  WebSurface (Playwright)          │
                     └────────────────────────────────────────────────────┘
```

**Four seams, each answering one requirement.** `Surface` is the only thing that knows a browser exists (3.7). The artifact is the only thing crossing discovery→replay (3.2/3.3). `SessionController` is the only thing that knows who is driving (3.6). The policy/redaction chokepoint is a mandatory crossing for every action and every byte written (3.4).

Three of those are enforced by `import-linter` contracts rather than convention, so "swap the surface" and "replay never touches the LLM" are properties CI checks, not claims in this document.

**Perception is accessibility-first.** The model reads roles and accessible names — `button "Search"`, `textbox "Member ID"` — never HTML. `ElementNode` has no field capable of holding a CSS selector or a DOM id; the type system forbids the mistake rather than a convention discouraging it. This is what makes the "no clean DOM" requirement honest, and it is why the same design extends to desktop: role-and-name is the one identity that exists in the browser accessibility tree, in Windows UI Automation, in macOS AX, and in a human operator's head.

**Key trade-offs.**

*A manual agent loop rather than the SDK's tool runner.* Every tool call must pass the policy engine, the control lease and the recorder before it reaches the surface, and that sequence is the safety story — it belongs somewhere a reviewer reads it in one place rather than behind framework callbacks. It also makes the model a swappable dependency, so the entire discovery path is exercised in CI with no key and no network.

*Files, not a database.* Artifacts are versioned documents that belong in git next to the code that runs them — that *is* the reviewability requirement in 3.2. A database would add migrations and remove `git diff` on a capability change. The brief also says explicitly that building scaling infrastructure is not rewarded.

*One process.* The production shape is a session broker owning browser contexts, stateless run workers, and an intervention queue. `Surface`, `SessionController` and the `Intervention` payload are already the interfaces those services would expose; building the queue would have cost a day and taught a reviewer nothing.

*A target application I wrote.* No public demo site produces record-not-found, permission denial, session timeout, an undeclared modal and a 500 on demand — and those are the interesting cases. It is deliberately hostile: framesets, nested layout tables, no test IDs, and ASP.NET control IDs regenerated on every render, so a recorded selector is dead on the second run.

**The obvious objection to that, stated before you have to ask it:** I built both the lock and the key, so I could have shaped one to fit the other. `git log -- targetapp/` is the honest answer and it is three commits long. The app was written first; the two changes since both tune *fault injection*, not the surface the automation reads — one scopes the surprise dialog to the member record rather than every screen, and one stops the expiry fault re-arming on each new session, which no real timeout does. I will concede the first makes the escalation demo tidier. Neither touched a locator, a label, or a control ID.

What is not tuned is the part that matters. Control IDs regenerate on every render, so a recorded selector is dead on the second run and the ladder has to work. And Lakeside was written to differ from Meridian *before* I knew what would break — when it turned out to render member detail as a definition list, that did not cost me an overlay, it invalidated my success conditions outright and forced a real fix to the compiler. An app shaped to flatter the automation would not have done that.

Where the convenience is real, and I would rather say so: I chose the *vocabulary*. A genuine legacy console would have unlabelled icon buttons, controls whose accessible name is empty, and text baked into images — and against those the ladder degrades to tier 6/7 or fails outright. That is the honest limit of this evidence, and it is why the tier telemetry is in the output: it is the measurement that would tell you, on a real system, how much of this held.

---

## Artifact schema

A capability is **a contract, not a macro**. A calling agent must answer four questions from the artifact alone: *what does this do, what do I pass, what do I get back, what can go wrong.*

```jsonc
{
  "id": "member.savings-balance", "version": "1.0.0",
  "status": "draft",                       // draft | approved | deprecated
  "risk_class": "read_only", "idempotent": true,
  "target": { "surface": "legacy-web",
              "product": {"vendor": "corelink", "app": "servicing-console"},
              "entry_url_pattern": "{{base_url}}/" },
  "inputs":  [{ "name": "memberId", "pattern": "^\\d{6}$", "sensitivity": "internal" }],
  "outputs": [{ "name": "savingsBalance", "parse": "currency",
                "sensitivity": "pii", "locator": { ... } }],
  "steps":   [{ "id": "s5", "intent": "submit the member search",
                "action": "click", "control_name": "Search", "risk": "read_only",
                "target": { "strategies": [ /* ranked ladder */ ] },
                "post_assert": [ ... ] }],
  "success_condition": { "kind": "output_present", "output": "savingsBalance" },
  "known_outcomes":  [ /* MEMBER_NOT_FOUND, PERMISSION_DENIED, ... */ ],
  "recoveries":      [ /* dismiss-known-interstitial, transient-load */ ],
  "provenance": { "model": "...", "prompt_version": "discovery/v1",
                  "surface_fingerprint": "..." },
  "stability":  { "replays": 0, "successes": 0 }
}
```

The decisions worth defending:

1. **`intent` on every step.** Someone approving automation that touches member money reads prose, not JSON. It doubles as the label in failure reports: *"step s4 expected the member detail screen, observed a sign-in form."*
2. **`target` is a ranked ladder, not a selector.** Robustness is a property of the *artifact*, so it is inspectable and diffable in review rather than hidden in the executor.
3. **`known_outcomes` are declarative data with their own detectors.** The brief calls conflating a business outcome with a crash "the most common design mistake here"; solving it at the schema layer means `MEMBER_NOT_FOUND` appears in the capability's published contract and a calling agent knows to handle it *before* it ever invokes the thing.
4. **`pre_assert` / `post_assert` per step, not one check at the end.** That is what turns "the run failed" into a diagnosis.
5. **Sensitivity declared per input and output.** Redaction is driven by the schema rather than by a regex hoping to catch everything.
6. **`secret_ref`, never a literal.** Credentials resolve from the environment at execution time, so committing one inside an artifact is structurally impossible.
7. **`risk_class` and `status` are orthogonal.** Risk is what the action does; status is how much this recording is trusted. Unattended execution of a write needs both.
8. **`idempotent` gates retry.** A non-idempotent step that times out mid-write is *ambiguous*, so replay escalates rather than retrying and double-posting.
9. **URLs canonicalised to `{{base_url}}` patterns**, so an artifact describes a vendor product rather than one tenant's deployment.
10. **Money is a string, never a float.** `"4182.55"`, parsed as `Decimal` if a caller needs arithmetic. `0.1 + 0.2` is the oldest bug in financial software and the type system is the only place to refuse it; JSON has one numeric type and it is binary floating point, so a `currency` output that deserialises to `float` has already lost. The `parse` field declares the intent, and the value crosses every boundary — artifact, log, API response — as text.
11. **`success_condition` names outputs rather than embedding their locators.** This one was forced by a bug: the first version embedded them, so a tenant that relocated a value passed every step and then failed its own success check against a locator the overlay had already replaced.

The result contract has **five variants**, not two:

```
success · business_outcome · escalated · blocked · failed
```

`business_outcome` is a peer of `success`, not a flavour of failure. `blocked` is separate from `failed` because a policy refusal is not a malfunction — the caller's response is "get approval", not "retry harder".

---

## Determinism & error handling

**How replay is made deterministic.** Six things, in rough order of how much work each does:

1. **Locators are re-derived, never replayed.** A recorded strategy is an *identity* — role, accessible name, and the data context the control sits in — and a selector is computed from it at run time. The target app regenerates every control ID on every render specifically to prove this matters.
2. **Uniqueness is required.** A strategy matching zero or two-or-more elements is skipped, never guessed at. Taking `.first` is how replay silently operates on the wrong member.
3. **Every wait is a condition with a timeout.** There is no `sleep()` in the replay package.
4. **Every step is guarded and verified**, so a run cannot silently drift off the recorded path.
5. **Recoveries are capped and counted.** Unbounded retry is how "deterministic" quietly becomes "eventually consistent" — and in a write flow, how you double-post.
6. **Outputs are parsed to declared types.** `"$4,182.55"` and `"4182.55 USD"` return the same `Decimal`, so a rendering difference is not a value difference.

**The locator ladder.** Seven tiers, best-first; at record time every strategy that *uniquely* resolves is kept.

| Tier | Strategy |
|---|---|
| 1 | role + exact accessible name, scoped to a section |
| 2 | role + normalised name (case/whitespace/punctuation-folded) |
| 3 | associated `<label>` text |
| 4 | data-grid row/column — *"the Action cell in the row where Member ID = 100042"* |
| 5 | text anchor — *"the value following the term 'Savings'"* |
| 6 | role ordinal within a section |
| 7 | bounding box |

Tier 4 is the one that earns its keep in legacy apps: the View link's accessible name is just "View" and is identical on every row, so the *row* has to be addressed by data. Tier 7 exists so the abstraction is not lying about Citrix-style surfaces where nothing semantic is available; it is ranked last and is never reached while anything better resolves.

Two safety rules came out of tests that failed:

- **Once a locator depends on a runtime parameter, every strategy that ignores that parameter is removed — not demoted.** An ordinal fallback on a parameterised target is not a fallback; it resolves confidently to whoever occupied that row when the flow was recorded. Bound to member 100043, the locator was resolving to member 100042.
- **A target's own column is excluded from its row key.** Otherwise reading a balance requires already knowing it, and the locator works exactly once.

**The error taxonomy.** After every action the observation goes to a classifier, and the evaluation order is the design:

| Class | Examples | Response | Result |
|---|---|---|---|
| **Business outcome** | `MEMBER_NOT_FOUND`, `PERMISSION_DENIED`, `VALIDATION_REJECTED`, `DUPLICATE_RECORD` | Return the declared code and payload. Not an error, does not raise. | `business_outcome` |
| **Declared failure** | `APP_ERROR` — the application's own 500 page | Recognised by its declared detector, reported through the failure channel | `failed` · `error_class: app_error` |
| **Recoverable** | known interstitial, transient slow load, expired session | Dismiss / wait / re-authenticate, bounded and logged | `success` with recoveries in the trace |
| **Needs a human** | undeclared dialog, recovery exhausted, ambiguous write, risky step | Freeze the session, raise an intervention | `escalated` |
| **Policy** | origin not allowed, risk gate | Refuse before touching the page | `blocked` |
| **Hard failure** | locator unresolved/ambiguous, checkpoint failed, extraction failed | Stop, dump evidence, report step + expected + observed | `failed` |

**The second row is the taxonomy arguing with itself, and worth the paragraph.** Declaring an outcome and *returning it as an answer* are two different things, and an application's own 500 needs the first without the second. A calling agent should learn from the contract that `APP_ERROR` can happen — that is what stops it treating every non-success as an outage. But there is no balance to report on an error page, so handing it back as a peer of `success` is the same conflation this taxonomy exists to prevent, pointed the other way: I would be telling a caller "the application returned a server error" and labelling it *a legitimate answer, not a failure*. `KnownOutcome.severity` carries the distinction. `info` and `warn` are answers; `error` is recognised by the same declared detector and then returned as `failed` with `ErrorClass.APP_ERROR` — which preserves "the app is broken" versus "the automation is broken" without pretending the app worked.

Two orderings are load-bearing. **Classification runs before post-conditions**, so an exceptional state is recognised as *itself* rather than as "the checkpoint failed" — get this backwards and "no such member" reaches the caller as a crash. And **policy is checked before the locator is resolved**, so nothing about a refused step ever touches the application.

Detection works from what is *on screen*, not from HTTP status codes: the failures that matter in these applications render as HTTP 200 with a red sentence on the page. Session expiry is the clearest case — the content frame swaps to a sign-in form and the top-level URL never changes, so anything watching the address bar sees a healthy run.

**Recovering is not the same as retrying**, and session expiry is where that stops being a distinction and starts being a bug. Signing in again lands on the *entry* screen, not on the one the step failed at, so repeating the failed step just fails again against a screen that no longer exists. The artifact declares `restart_from_step`, the recovery re-enters through the front door, and the run picks up from there — bounded, because a flow that keeps being sent back to sign-in is looping rather than recovering, and in a write flow each pass could post again. There is no bespoke "re-authenticate" action: the sign-in steps are already the first steps of the capability, so restarting from them *is* re-authenticating, and a second copy of the credentials and the login form's locators would only rot.

All ten scenarios are in `uv run cua eval` and in [`evidence/demo/eval-matrix.txt`](evidence/demo/eval-matrix.txt).

**The one place "I don't know" is the correct answer.** An irreversible step that does not come back clean leaves the write in an *unknown* state — the POST may have committed before the error page rendered, and no amount of reading the screen settles it, because the screen is the error. Every ordinary response is wrong: reporting `APP_ERROR` tells the caller nothing happened, retrying opens the account twice, and failing loses the fact that a write is outstanding. So replay escalates as `AMBIGUOUS_WRITE_OUTCOME` and asks a human to check the system of record. This is scoped to *irreversible* steps only — `DUPLICATE_RECORD` arrives one step earlier from the review screen, where the application refused *before* committing, and that must stay a clean business outcome or a non-idempotent capability stops being safe for a caller to retry at all. Recording the distinction between "nothing happened" and "I cannot tell" is the entire practical value of the `idempotent` field.

The evidence proves it rather than asserting it. In the target app the write-timeout fault **commits and then loses its acknowledgement** — an earlier version failed *before* writing, which made the case safe and the demonstration worthless. Now `write-5-ambiguous-outcome` escalates, and `write-6-the-write-had-landed` is what a human finds on inspection: `DUPLICATE_RECORD`. The account had been opened. Reporting `APP_ERROR` — which is what the screen literally says — would have invited a retry and given the member two sub-accounts.

**Drift**, secondarily. Every run records which tier carried each step. A step recorded at tier 1 that starts resolving at tier 4 means a label changed — the earliest available warning, per tenant, for one integer per step. Each run also fingerprints the entry screen's role/name skeleton and flags a mismatch without failing the run. `cua verify` sweeps every tenant nightly, executing only steps the artifact classifies `read_only` and stopping at the first write.

---

## Heterogeneity & multi-tenant

**Surfaces.** The seam is `Surface`; an artifact contains no browser concepts, only roles, names, data context and a frame path.

| Surface | Perception | What changes in the artifact |
|---|---|---|
| Modern web | aria snapshot + DOM enrichment | nothing |
| **Legacy web** (framesets, tables, no test IDs) | same; tiers 4 and 5 do most of the work | nothing — `frame_path` already exists. **This is what was built.** |
| **Desktop** (Win32/WPF) | UI Automation tree → the same `ElementNode` (`ControlType`≈role, `Name`≈accessible name) via `uiautomation`/FlaUI | `surface: "desktop"`, `frame_path` → window path. Tiers 1–3 and 5 map directly; tier 4 maps to UIA's Grid pattern. |
| Terminal / 3270 | field map → `ElementNode` with screen coordinates | tiers 5 and 7 survive; the ladder degrades without breaking |
| Citrix / pixel-only | OCR + vision | only tier 7. The honest answer: this is the degenerate case, and it should require a higher `stability` score before approval. |

**Multi-tenant.** ~100 institutions × ~20 apps is ~2,000 instances but only ~20 *products*, so the unit of authoring has to be the product. A capability targets `{vendor, app, version_range}`; a tenant profile is a thin diff composed at replay time. The artifact on disk is never mutated — `git diff` on a capability shows a change to the product automation, `git diff` on a tenant profile shows one institution's specialisation. Merging them would make both unreviewable.

This is demonstrated, not asserted. The shipped capability was recorded against Meridian and replays unmodified at Lakeside, which renames the search button, renames the ID field, **requires an extra step**, and renders balances as a definition list instead of a table. The overlay is 2 step overrides + 1 inserted step + 1 output override against a 7-step flow.

Two constraints make it safe:

- **Overrides prepend strategies rather than replacing them.** The recorded ladder stays underneath as a fallback, so a tenant that quietly reverts to standard labelling keeps working — and the tier telemetry shows which tenants actually depend on their override.
- **No overlay may redefine `success_condition`.** A tenant may differ in how it labels a button, not in what the capability *means*. A capability that means different things at different institutions is not one capability.

**Managing drift at scale**: nightly `verify` sweep per tenant → tier-degradation and unresolved counts → a tenant whose step stops resolving gets an override PR, not a re-recording. Only when a step fails across *all* tenants has the vendor shipped a new version, and the base recording is bumped.

---

## Escalation & handoff

**Detecting stuck.** Six triggers: the model calling `request_human`, an observation-fingerprint no-progress detector, recovery exhausted, an undeclared dialog, a risky step needing approval, and an ambiguous locator.

**Routing.** An `Intervention` carries what the brief asks for — capability, goal, current step *and its recorded intent*, why it stopped, the URL, a masked screenshot, and the aria snapshot — plus two things an operator needs to *act* rather than merely understand: what was already tried, and the realistic options (every list ends with *abort*). Locally it is a file and a printed URL; in production it is a queue message. **The payload is identical**, which is the point of defining it as a model.

**Taking control.** `SessionController` is a **lease**, and three properties make it more than a flag:

- **Exclusivity raises.** The guard is injected into `WebSurface.act()`, so the prohibition sits below anything that might forget to check it.
- **A monotonic epoch invalidates in-flight work.** This is the real race: an automation action can be mid-`await` when a human takes over, and without an epoch it lands *after* the transfer on a page the operator is editing. A boolean cannot express that.
- **Ceding parks a coroutine; nothing is torn down.** Cookies, auth, scroll position, a half-filled form and the frame tree all survive, because it is literally the same page object. This is why the project uses the async Playwright API — the sync one cannot suspend and keep serving the console.

The console streams the live session over CDP (`Page.startScreencast`) and forwards clicks and keystrokes back (`Input.dispatchMouseEvent`), so an operator needs nothing but a browser tab and it works against a headless run on a server. Viewing is ungated; input requires the lease. Everything the operator does is captured through an init-script listener into the *same* event stream as automation's own actions, distinguished by an `actor` field.

**Handing back is a re-orientation, not a jump.** The operator may have done more, less, or something else than asked, so their stated disposition is a **claim** and the screen is the **evidence**:

1. Re-classify. If the blocking condition is still present, ask again rather than believing them.
2. Success condition satisfied → done, recorded as `completed_by: "human"`.
3. This step's post-condition satisfied → the operator did it; advance.
4. Its pre-condition satisfied → retry the step.
5. None of the above → raise a fresh intervention rather than guess.

That ordering caught a real bug: an operator who claimed "I completed the step" without acting got a `success`, balance read, with an unanswered regulatory hold notice still on screen.

**The same machinery answers a second question.** Replay escalates because it is *stuck*. Discovery escalates because it is about to do something *irreversible* — and until late in the build it had nowhere to escalate to, so the run simply ended one step before the only step that mattered, and a write flow could not be recorded at all. `cua discover --operator-port` gives discovery the same lease, the same intervention store and the same console: the operator is asked to authorise one named action, and answers yes or no.

Two details earn their place. Automation performs its **own** proposed action once authorised, rather than the operator clicking it — so the recorded step keeps the model's stated intent, and an artifact that replays comes out instead of a note that something once happened. And with no console attached the answer is **no**: an unattended recording session must not be able to open an account merely because nobody was watching.

**Cut**: operator identity and authz on every route, per-tenant routing and SLAs, multiple concurrent sessions, and recording an operator's fix as a proposed artifact patch for review — the obvious flywheel, and the thing I would build next.

---

## Safety

**Two enforcement layers.** *Layer 1* checks every action from every actor against a configurable allowlist of origins, paths and action types. *Layer 2* is Playwright request interception that aborts anything heading to a non-allowlisted origin.

The second is not redundant, and the distinction is the point: **layer 1 governs what the agent chooses to do; layer 2 governs what the page does on the agent's behalf.** A 302 to an external identity provider, or a beacon injected into a legacy app's free-text field, involves no action at all. Prompt injection through application data is a real threat here, and only the network layer stops it. There is a test that injects a tracking pixel carrying a member ID and asserts it is blocked.

**Policy is data**, in YAML, so a compliance reviewer can read what the agent may do without reading Python — and a change to it is a reviewable diff. A capability declares its own limits, which are *intersected* with the global policy: an artifact can only ever narrow, never widen.

**Risk is judged from what a control says**, because that is what a human operator reads before deciding. Classification happens at *compile* time and is written into the artifact, so the person approving a capability sees the same judgement the runtime will apply; an artifact may declare a step stricter than inferred, never looser. Typing is never a write — the submit is.

**Irreversible writes fail closed**, requiring `status == approved` **and** an explicit caller approval. During *discovery* the same action escalates to a human instead: the model is least trustworthy exactly when it is exploring a UI it does not yet understand, and reusing the escalation path means one mechanism covers two rules.

**Redaction** has a declared-field primary and a pattern backstop, applied at every egress — logs, artifacts, screenshots (masked *before* the PNG is encoded), interventions, and the prompts themselves. It is judged on false positives too: member IDs and confirmation numbers stay readable, and card numbers are Luhn-checked, because a redactor that masks everything makes evidence unreadable and therefore unread.

Four leaks were found by tests during the build, all of the same shape — *the accessible name of a value-bearing element is the value*. A password reached the aria snapshot sent to the model; a credential was compiled into an artifact as a step literal; a balance appeared in a locator's human-readable description; and a balance was in the recorded transcript's replay hint. Each is now referenced by position or by name.

**Limits, stated plainly.** A full-page screenshot can still capture PII in fields nobody declared — masking works from declared locators. Pattern matching cannot recognise an account number that looks like any other integer. Nothing can un-send a value that already reached the model, which is why sensitive parameters are substituted locally and never appear in a prompt. The allowlist protects the *agent's* actions and the page's network egress; it does not sandbox the browser process. Production would add field-level classification from the application catalogue, a DLP scan over the evidence directory in CI, and a real secret manager behind `secret_ref`.

---

## Cuts

**Deliberately not built:**

- **A second *live-recorded* capability.** The shipped artifact *was* recorded by `gpt-4o` against the live API, and the transcript in `evidence/fixtures/` is that run — so `--mock` replays a real model, not a stand-in. Only one capability has been through the live loop.
- **An MCP server in front of the catalogue.** `cua catalog` *is* built — every artifact is rendered as a tool definition, with its declared business outcomes in the description so a calling agent knows `MEMBER_NOT_FOUND` is a possible answer before it invokes. What is not built is the MCP transport that would let a model discover it over a wire; the contract is generated, the serving of it is a shim.
- **Desktop and terminal surfaces.** Designed against the `Surface` ABC, not implemented. The mapping table above is the deliverable.
- **Assisted LLM recovery on replay failure.** The policy envelope is designed — one step, bounded, policy-checked, recorded as evidence — but a bounded model call inside the deterministic path needs more care than a week allows.
- **Infrastructure**: queues, workers, a database, Docker, multi-tenant plumbing. The brief says explicitly this is not rewarded, and the seams that would become service boundaries already exist.
- **Operator identity and authz.** The console has no login. Fine for a local demo, unacceptable in production, and the fix is ordinary.
- **`preflight` and `auth_capability`.** Two schema fields with no implementation behind them, kept because they name the shape of something real and cut because inventing a use for them here would be worse than leaving them declared. `preflight` is where a duplicate check belongs — the sub-account flow already does one server-side, and hoisting it into the artifact is what would let a caller safely retry a *capability* that must never retry a *step*. `auth_capability` is what sign-in becomes once it is shared: right now re-authentication is expressed as `restart_from_step` pointing at this capability's own first step, which works precisely because sign-in is inlined into every recording. The moment two capabilities share a session, that inlining is the thing you would remove.

**Two schema fields that were dead and are not any more**, because a field nobody feeds is worse than no field: `stability` is now written by the `verify` sweep, so `cua approve` reports real evidence instead of `0/0`; and `restart_from_step` is what makes session-expiry recoverable rather than an escalation.

**What the live run changed.** Three bugs only a real model surfaced, all of them cases where the scripted stand-in had been quietly generous:

- *The model had no way to sign in.* It navigated, saw a login form, and escalated — correctly. The stand-in "knew" the credentials because I had written them into the test plan. Fixed by telling the model it can type `<secret:NAME>` placeholders, which the agent substitutes locally: the model authenticates **without ever receiving a credential**, so it cannot leak one into a transcript, a log, or its own context.
- *The no-progress detector stopped a healthy run.* It compared only the screen fingerprint, which ignores field values by design — so a model filling in a login form looked identical to one clicking a dead button. Thrashing is now defined as the same action on the same element, not merely a screen that has not moved.
- *Assertions never bound input parameters.* The live model recorded a checkpoint on "the cell showing `{{memberId}}`" — a perfectly reasonable thing to record. Every action resolved, and then the success condition failed hunting for a cell literally named `{{memberId}}`. Only action resolution had been binding parameters.

Each is now covered by a test written from the failure. The general lesson is the one worth carrying: **a stand-in tests the code you wrote, not the assumptions you made.**

**What I would build next, in order:**

1. **A diff-and-review UI for artifacts.** The weakest part of this system is discovery quality — the compiler's step-pruning and auto-parameterisation are heuristics, and on a genuinely complex flow I would expect to hand-edit an artifact before approving it. That is *why* `status: draft` exists, and a review surface is what makes the draft→approved gate real rather than ceremonial.
2. **Turn an operator's fix into a proposed overlay.** Every handoff already records what the human did. Converting that into a reviewable tenant override closes the loop between escalation and drift management, and is what stops the escalation rate growing with the tenant count.
3. **The nightly conformance sweep as a service**, with per-tenant dashboards and alerting on tier-degradation rate — the leading indicator that something is about to break.
4. **Recording write flows unattended, safely.** Right now a human must authorise every risky action during discovery, which is correct and does not scale: recording a hundred capabilities means a hundred sessions with somebody watching. The shape of the answer is a *rehearsal* mode — drive the flow against a sandbox tenant where the write is a no-op, then require one human sign-off on the compiled artifact rather than one per click.
