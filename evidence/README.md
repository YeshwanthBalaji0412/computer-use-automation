# Evidence

Real runs against the target application, committed so a reviewer can read the output
without running anything. Reproducible from a clean checkout:

```bash
uv run cua serve-app                          # terminal 1
uv run python scripts/build_evidence.py       # terminal 2
```

No API key is needed — discovery replays the recorded `gpt-4o` transcript in `fixtures/`,
and replay never calls a model at all. The build script **fails loudly** if a credential or
a member's balance appears in anything it is about to commit.

Start the app fresh. These runs open a real sub-account in the target app's in-memory
fixtures, so a second build against the same process would legitimately return
`DUPLICATE_RECORD` where the first returned `success` — which is the app being correct, and
the reason the *eval matrix* deliberately contains no scenario that creates anything.

`demo/` is committed. `runs/` and `fixtures/` are working directories: `runs/` is
gitignored scratch, `fixtures/` holds the recorded transcript that `--mock` replays.

---

## Start here

| # | Path | What to look at |
|---|---|---|
| 1 | [`demo/member.savings-balance@1.0.0.json`](demo/member.savings-balance%401.0.0.json) | The artifact. Typed inputs and outputs, per-step `intent`, ranked locator ladders, declared business outcomes. |
| 2 | [`demo/discovery/`](demo/discovery/) | How it was produced by `gpt-4o` — and note that `transcript.json` is kept *separate* from the artifact. |
| 3 | [`demo/eval-matrix.txt`](demo/eval-matrix.txt) | Thirteen scenarios, nine different answers, one screen. |
| 4 | [`demo/replay-escalated-handoff/console.txt`](demo/replay-escalated-handoff/console.txt) | A run parked, a human resolved it, the run finished. |
| 5 | [`demo/write-5-ambiguous-outcome/`](demo/write-5-ambiguous-outcome/) | The hardest case in the system: a write that may or may not have landed. |
| 6 | [`demo/catalog.txt`](demo/catalog.txt) | Both capabilities as tools an agent could be handed, failure modes included. |

---

## The runs

### The write flow, in order

These five are **sequential**, not independent — the duplicate refusal only means anything
after the account has been opened. Read them top to bottom.

| Directory | Condition | Result |
|---|---|---|
| [`write-1-blocked-unapproved`](demo/write-1-blocked-unapproved/) | a `draft` irreversible capability | `blocked` · needs *both* gates |
| [`write-2-blocked-no-caller-approval`](demo/write-2-blocked-no-caller-approval/) | after `cua approve` | `blocked` · needs only `--approve` |
| [`write-3-success`](demo/write-3-success/) | approved **and** `--approve` | `success` + confirmation number |
| [`write-4-duplicate`](demo/write-4-duplicate/) | the identical request again | `business_outcome` · `DUPLICATE_RECORD` |
| [`write-5-ambiguous-outcome`](demo/write-5-ambiguous-outcome/) | the confirm hangs, then 500s | `escalated` · `ambiguous_write_outcome` |

The first two are the same refusal with one gate removed, and that is the point:
`risk_class` is a property of the action, `status` is how much this *recording* is trusted,
and `--approve` is this *caller* accepting the consequence. Three decisions, three people,
three moments.

The last one is the only place in this system where **"I don't know" is the correct
answer**. The write may or may not have committed before the error page rendered, and the
screen cannot tell you which — the screen *is* the error. Compare it against
`write-4-duplicate`, which says *nothing was created*: that is a definitive answer from one
step earlier, where the application refused before committing. Conflating the two is how
you either double-open an account or tell a member nothing happened when it did.

### The read flow

| Directory | Condition | Result |
|---|---|---|
| [`replay-success`](demo/replay-success/) | the recorded flow | `success` + typed outputs |
| [`replay-not-found`](demo/replay-not-found/) | member 999999 | `business_outcome` · `MEMBER_NOT_FOUND` |
| [`replay-permission-denied`](demo/replay-permission-denied/) | a restricted record | `business_outcome` · `PERMISSION_DENIED` |
| [`replay-slow-recovered`](demo/replay-slow-recovered/) | a screen slower than one settle window | `success`, after a bounded re-observe |
| [`replay-app-error`](demo/replay-app-error/) | the app's own 500 page | recognised and reported |
| [`replay-escalated-handoff`](demo/replay-escalated-handoff/) | an undeclared dialog | parked → human → `success` |
| [`discovery-write-flow`](demo/discovery-write-flow/) | recording a write, attended | two irreversible actions authorised by a named human |
| [`replay-lakeside`](demo/replay-lakeside/) | the same artifact at another institution | `success`, with the overlay visible in the telemetry |

Note that **not-found and permission-denied are different codes**. One means the record
does not exist; the other means it does and this operator may not see it. Conflating them
is a compliance problem, not a bug.

---

## What is in a run directory

```
events.jsonl     one JSON object per event, in order, with an `actor` field
manifest.json    what ran, with what inputs (redacted), and how it ended
report.md        the same thing rendered for a human
steps/*.png      per-step screenshots, masked before encoding
trace.zip        Playwright trace: npx playwright show-trace trace.zip
failure/         on failure or escalation: aria snapshot, observations, summary
interventions/   the intervention record and its resolution
console.txt      what the operator saw in their terminal
```

`trace.zip` is the one to open when a step fails for a reason the aria snapshot does not
explain. It carries the DOM at every action, the network log, and the console — the layer
this system deliberately does *not* look at during normal operation, which is exactly why
it is worth having when perception and reality disagree.

Every run *produces* one; only five are **committed** — the hard failure, the handoff, the
cross-tenant run, the ambiguous write, and one recording. A trace costs about a megabyte in
a repository someone has to clone, and a trace of a run that did exactly what it was told
demonstrates only that traces exist, which those five already do. `build_evidence.py`
drops the rest, and your own runs under `runs/` keep theirs.

Two things worth opening:

**`events.jsonl` records why, not just what.** Every acting tool requires a `why`
argument, which becomes the step's `intent` in the artifact and the label here. A trace
that says `clicked e11` is useless six weeks later; `submit the member search` is not.

**`failure/aria.txt` is what the system *perceived*.** Put next to the screenshot of what
it *looked like*, the difference between the two is usually the explanation.

---

## The handoff, narrated

From [`replay-escalated-handoff/console.txt`](demo/replay-escalated-handoff/console.txt):

```
intervention  unknown_dialog at s6
  why         a dialog titled 'Regulation CC Hold Notice' appeared and is not
              declared in this capability's recoveries or known outcomes
  lease       human (epoch 1)
  operator    dismissed the hold notice
  recorded    ["click button 'Acknowledge'"]
  resumed     as 'recovered'

status     success
outputs    {"savingsBalance": "4182.55"}
```

Four things are happening there. The system stopped because it hit something *nobody had
declared* — not because anything broke. The **lease** moved to the human and the epoch
advanced, so automation could not act while they held it. The operator worked in the
*same* browser session, and what they did was captured into the same event stream as
automation's own actions. And on resume, automation re-read the screen rather than taking
their word for it.

The corresponding `events.jsonl` shows two `control_transferred` events — to `human` and
back to `automation` — with the disposition recorded on the second.

---

## Cross-tenant

[`replay-lakeside/console.txt`](demo/replay-lakeside/console.txt) is the same artifact,
recorded against Meridian, replayed at an institution that renames the search button,
renames the ID field, requires a branch selection first, and renders balances as a
definition list instead of a table:

```
status     success
outputs    {"savingsBalance": "4182.55"}
drift      suspected - the screen's shape differs from the recording
locators   2 step(s) resolved at a lower tier than recorded
```

It did not merely work — it reported **which two steps leaned on the overlay**.
[`conformance-sweep.txt`](demo/conformance-sweep.txt) shows the same signal as a nightly
check across every tenant:

```
TENANT        STEPS  DEGRADED  UNRESOLVED  NOTES
lakeside          8         2           0  s4:t1->t3, s7:t4->t5
meridian          7         0           0  clean
```

Degraded steps still work; they are the early warning. Unresolved steps are what will
fail next.

---

## On the data

Every member, name, balance and credential in here is invented. `100042` and `J. RIVERA`
are fixtures in [`targetapp/data.py`](../targetapp/data.py). Nothing was automated against
a third-party site and no real credentials were used anywhere.

The demo password appears in **no committed file, anywhere**. A member's balance appears
in nothing the system *persists* — not the artifact, not the transcript, not an
intervention record, not an event log, not a manifest, not a failure dump.

It does appear in `console.txt`, and that is the correct behaviour rather than a gap. The
capability's declared output *is* a savings balance; a run that refused to print it to the
operator who asked for it would not have done its job. Redaction here means no
**incidental retention** — the value must not survive anywhere it was not explicitly
requested — and drawing that line precisely matters more than drawing it conservatively,
because a rule stated too broadly is one nobody can actually enforce.

Which is the point of [`scripts/check_secrets.py`](../scripts/check_secrets.py): those
three rules are encoded there, run in CI on every push and again at the end of every
evidence build. Not checked by hand, and not taken on trust from this paragraph.
