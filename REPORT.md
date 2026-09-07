# Design write-up

An LLM learns a legacy banking UI once; a typed artifact replays it forever with no model in the loop. This is the argument; the working detail is in [`evidence/`](evidence/) and the code comments.

## Architecture

Four seams, each answering one requirement:

| Seam | Answers |
|---|---|
| `Surface` ABC — the only Playwright importer | heterogeneity |
| The capability artifact — the only thing crossing discovery→replay | reusability |
| `SessionController` — the only holder of the control lease | handoff |
| `PolicyEngine` + `Redactor` — a mandatory chokepoint | safety |

Three are enforced by `import-linter` contracts rather than convention: replay may not import `openai` or `cua.discovery`, and Playwright may not escape `surface/`. "Replay runs without the LLM" is a requirement, so CI proves it instead of the README claiming it.

**Perception is accessibility-first** — `aria_snapshot()` enriched with DOM context into `ElementNode` (role, accessible name, frame path, row context). There is no CSS or XPath field anywhere in the schema. That is the same information Windows UI Automation and macOS AX expose, which is why the design ports.

**Trade-offs.** A manual agent loop, not the SDK's tool runner: every call passes policy, lease and recorder before the surface, and that sequence *is* the safety story. Files, not a database — artifacts belong in `git diff`. One process; the seams above are where services would split. A target app I wrote, because no public site yields permission denial, session timeout and a 500 on demand.

## Artifact schema

A capability is **a contract, not a macro**: a calling agent must answer *what does this do, what do I pass, what comes back, what can go wrong* from the artifact alone.

```jsonc
{ "id": "member.savings-balance", "version": "1.0.0",
  "status": "draft", "risk_class": "read_only", "idempotent": true,
  "target": { "product": {"vendor":"corelink","app":"servicing-console"},
              "entry_url_pattern": "{{base_url}}/" },
  "inputs":  [{ "name":"memberId", "pattern":"^\\d{6}$", "sensitivity":"internal" }],
  "outputs": [{ "name":"savingsBalance", "parse":"currency", "sensitivity":"pii" }],
  "steps":   [{ "id":"s5", "intent":"submit the member search", "action":"click",
                "risk":"read_only", "target":{"strategies":[ /* ranked ladder */ ]},
                "post_assert":[ ... ] }],
  "success_condition": { "kind":"output_present", "output":"savingsBalance" },
  "known_outcomes": [ /* MEMBER_NOT_FOUND … each with its own detector */ ] }
```

- **`target` is a ranked ladder, not a selector.** Robustness becomes a property of the artifact — inspectable and diffable in review, not hidden in the executor.
- **`known_outcomes` are declarative data.** `MEMBER_NOT_FOUND` appears in the published contract, so a caller knows to handle it *before* invoking.
- **`intent` on every step.** Whoever approves automation over member money reads prose, and it doubles as the failure label: *"s4 expected the member detail screen, observed a sign-in form."*
- **`risk_class` and `status` are orthogonal.** Risk is what the action does; status is how far this *recording* is trusted. An unattended write needs both, plus a per-call approval.
- **Money is a string, never a float.** JSON has one numeric type and it is binary floating point.
- **`success_condition` names outputs, never their locators** — forced by a bug where a tenant that relocated a value passed every step, then failed its own success check.

The result contract has **five variants**: `success · business_outcome · escalated · blocked · failed`. `business_outcome` is a peer of success; `blocked` is distinct from `failed` because a policy refusal is not a malfunction.

## Determinism & error handling

**Locators are re-derived, never replayed.** A recorded strategy is an *identity*; the selector is computed at run time from a seven-tier ladder, and **a tier matching more than one element is discarded, not disambiguated by position**. The target app regenerates every control ID per render to prove this matters. Two rules came from failing tests: once a locator depends on a parameter, strategies ignoring it are *removed, not demoted* — an ordinal fallback resolves confidently to whoever occupied that row at record time; and a target's own column is excluded from its row key, or reading a balance requires already knowing it.

| Class | Response | Result |
|---|---|---|
| Business outcome — `MEMBER_NOT_FOUND`, `DUPLICATE_RECORD` | return the declared code | `business_outcome` |
| Declared failure — `APP_ERROR` | declared, but reported as a failure | `failed` · `app_error` |
| Recoverable — interstitial, slow load, expired session | dismiss / wait / re-authenticate, bounded | `success` + recoveries |
| Needs a human — undeclared dialog, ambiguous write | freeze, raise an intervention | `escalated` |
| Hard failure — locator unresolved, checkpoint failed | stop, dump evidence | `failed` |

Row two is the taxonomy arguing with itself. A 500 must be *declared*, so an agent doesn't treat every non-success as an outage — but it is not an *answer*, and returning it as a peer of success is the brief's named mistake pointed the other way. `KnownOutcome.severity` carries the split.

**Classification runs before post-conditions**, so an exceptional state is recognised as itself rather than "the checkpoint failed"; **policy is checked before the locator resolves**, so a refused step never touches the application. Detection reads the screen, not HTTP status — session expiry swaps the content frame while the top-level URL never changes. And **recovering is not retrying**: re-authenticating lands on the entry screen, not the failed step, so the artifact declares `restart_from_step`.

**The one place "I don't know" is correct.** An irreversible step that doesn't come back clean may have committed before the error rendered. `APP_ERROR` understates it, retrying double-posts, failing loses the outstanding write — so it escalates. The evidence proves this rather than asserting it: the fault **commits and then loses its acknowledgement**, and `write-6` is a human checking the record and finding `DUPLICATE_RECORD`. It *had* been opened.

Fourteen scenarios, nine distinct answers: `uv run cua eval`.

## Heterogeneity & multi-tenant

The `Surface` ABC deals in roles, accessible names and row context — never markup. A `DesktopSurface` over UIA or AX is a new implementation of one interface, because role-plus-name is precisely what those APIs expose. Screenshot-plus-coordinates is the tier-7 fallback for Citrix-style surfaces: ranked last, never reached while anything semantic resolves, and present so the abstraction isn't lying about that case.

**Artifacts target a vendor *product*, not a tenant.** URLs canonicalise to `{{base_url}}`, so ~2,000 app instances collapse to ~20 products. A tenant profile is a thin diff composed at replay — renamed controls, extra steps, relocated outputs. Three constraints keep it honest: overrides *prepend* strategies rather than replacing them, so the base ladder survives as fallback; no overlay may redefine `success_condition`, or a tenant could quietly redefine "worked"; and an overlay can only narrow policy, never widen it.

Demonstrated, not merely designed: the Meridian recording replays unmodified on Lakeside, which renames the search button and the ID field, demands a branch selection first, and renders balances as a definition list. It reports **which two steps leaned on the overlay**. Every run records the tier that carried each step, so a step drifting tier 1→4 is the earliest available warning, per tenant, for one integer. `cua verify` sweeps every tenant nightly, running only `read_only` steps.

## Escalation & handoff

Six triggers: `request_human`, a no-progress detector keyed on *(action, element, screen)*, recovery exhausted, an undeclared dialog, a risky step, an ambiguous locator. The `Intervention` carries the capability, goal, current step and its recorded intent, why it stopped, a masked screenshot and the aria snapshot — plus what was already tried and the realistic options. Locally that is a file and a printed URL; in production a queue message. **The payload is identical**, which is the point of modelling it.

`SessionController` is a **lease**, not a flag. Exclusivity is enforced inside `WebSurface.act()`, below anything that might forget to check. A monotonic **epoch** invalidates in-flight work — an action can be mid-`await` when a human takes over, and a boolean cannot express that. Ceding parks a coroutine: cookies, auth and a half-filled form survive because it is the same page object, which is why this uses the async Playwright API. The console streams that session over CDP and forwards input back; everything the operator does lands in the same event stream, tagged by `actor`.

**Handing back is re-orientation, not a jump.** The operator's stated disposition is a *claim*; the screen is the evidence. Re-classify first, then check the success condition, the post-condition, the pre-condition — and raise a fresh intervention rather than guess. That ordering caught a real bug: an operator claiming "I completed the step" without acting got a clean `success`, balance read, with the hold notice still on screen.

The same machinery serves discovery, where escalation means *about to do something irreversible* rather than *stuck*. With no console attached the answer is no — an unattended recording session cannot open an account merely because nobody was watching.

## Safety

**Two enforcement layers.** Layer 1 checks every action against a configurable allowlist of origins, paths and action types. Layer 2 is request interception. The second is not redundant: **layer 1 governs what the agent chooses to do; layer 2 governs what the page does on its behalf.** A 302 to an external identity provider, or a beacon injected into a legacy free-text field, involves no agent action at all — prompt injection through application data is a real threat here, and only the network layer stops it.

Policy is YAML, so a compliance reviewer reads it without reading Python. A capability's own limits are *intersected* with the global policy, never unioned. **Risk is judged from what a control says**, at compile time, so the approver sees the judgement the runtime will apply. **Irreversible writes fail closed**, needing `status == approved` *and* a per-call approval.

**Redaction** runs a declared-field primary with a pattern backstop at every egress — logs, artifacts, screenshots masked *before* PNG encoding, and the prompts themselves. It is judged on false positives too: member IDs stay readable, because a redactor that masks everything produces evidence nobody reads. Four leaks were caught by tests during the build, all the same shape — *the accessible name of a value-bearing element is the value*.

**Limits.** A full-page screenshot can still capture PII in undeclared fields. Pattern matching cannot recognise an account number that looks like any other integer. The allowlist protects the agent's actions and the page's egress; it does not sandbox the browser process. Production needs field-level classification from the application catalogue and a real secret manager behind `secret_ref`.

## Cuts

**Not built:** a second *live-recorded* capability — the read flow was recorded by `gpt-4o` and its transcript is what `--mock` replays; the write flow's plan is deterministic. An MCP transport in front of the catalogue (`cua catalog` emits the tool definitions; only the wire is missing). Desktop and terminal surfaces. Bounded LLM recovery on replay failure. Queues, workers, Docker. Operator identity — the console has no login, fine locally and unacceptable in production.

**Declared but unimplemented:** `preflight`, where a duplicate check belongs, and `auth_capability` — what sign-in becomes once two capabilities share a session. Today it is inlined into every recording, which is exactly why `restart_from_step` works as re-authentication.

**Next, in order:** a diff-and-review UI for artifacts, because discovery quality is the weakest link and `status: draft` means little without a review surface; turning an operator's fix into a proposed tenant override; the conformance sweep as a service, alerting on tier-degradation rate; and a rehearsal mode, so write flows record without a human authorising every click.

The general lesson, learned from three bugs only a live model found: **a stand-in tests the code you wrote, not the assumptions you made.**
