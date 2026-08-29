# Evidence

Real runs against the target application, committed so a reviewer can read the output
without running anything. Reproducible from a clean checkout:

```bash
uv run cua serve-app                          # terminal 1
uv run python scripts/build_evidence.py       # terminal 2
```

No API key is needed — discovery replays the recorded `gpt-4o` transcript in
`fixtures/`, and replay never calls a model at all. The build script **fails loudly** if a credential or a member's balance appears in
anything it is about to commit.

`demo/` is committed. `runs/` and `fixtures/` are working directories: `runs/` is
gitignored scratch, `fixtures/` holds the recorded transcript that `--mock` replays.

---

## Start here

| # | Path | What to look at |
|---|---|---|
| 1 | [`demo/member.savings-balance@1.0.0.json`](demo/member.savings-balance%401.0.0.json) | The artifact. Typed inputs and outputs, per-step `intent`, ranked locator ladders, declared business outcomes. |
| 2 | [`demo/discovery/`](demo/discovery/) | How it was produced by `gpt-4o` — and note that `transcript.json` is kept *separate* from the artifact. |
| 3 | [`demo/eval-matrix.txt`](demo/eval-matrix.txt) | Ten scenarios, six different answers, one screen. |
| 4 | [`demo/replay-escalated-handoff/console.txt`](demo/replay-escalated-handoff/console.txt) | A run parked, a human resolved it, the run finished. |

---

## The runs

| Directory | Condition | Result |
|---|---|---|
| [`replay-success`](demo/replay-success/) | the recorded flow | `success` + typed outputs |
| [`replay-not-found`](demo/replay-not-found/) | member 999999 | `business_outcome` · `MEMBER_NOT_FOUND` |
| [`replay-permission-denied`](demo/replay-permission-denied/) | a restricted record | `business_outcome` · `PERMISSION_DENIED` |
| [`replay-slow-recovered`](demo/replay-slow-recovered/) | a screen slower than one settle window | `success`, after a bounded re-observe |
| [`replay-app-error`](demo/replay-app-error/) | the app's own 500 page | recognised and reported |
| [`replay-escalated-handoff`](demo/replay-escalated-handoff/) | an undeclared dialog | parked → human → `success` |
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
failure/         on failure or escalation: aria snapshot, observations, summary
interventions/   the intervention record and its resolution
console.txt      what the operator saw in their terminal
```

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

The balance and the demo password do not appear in any committed file — not in the
artifact, not in the transcript, not in an intervention record, not in a log. That is
checked by the build script rather than by hand.
