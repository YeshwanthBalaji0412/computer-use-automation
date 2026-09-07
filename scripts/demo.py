"""A guided walkthrough of the system, for a demo or a screen recording.

    uv run cua serve-app          # terminal 1, leave running
    uv run python scripts/demo.py # terminal 2

Runs the segments in order, printing what each one is about to prove before it runs it and
what to look at afterwards. It pauses between segments so you can talk over them; pass
`--auto` to run straight through, or `--only 5` to rehearse one segment.

It resets first, because two pieces of state drift between takes and both spoil a segment
quietly rather than loudly:

* the write capability ends up `approved`, so the first replay reports one missing
  approval gate instead of two - and the two-gate chain is the point of that segment;
* the target app accumulates sub-accounts in memory, so a nickname that opened cleanly on
  the last take returns `DUPLICATE_RECORD` on this one, which looks like a bug rather than
  the feature it is.

There is deliberately no reset *endpoint* on the app. A real core banking system does not
have one, and building a demo around something that cannot exist in production would prove
the wrong thing - the same reason the eval matrix uses a unique key per run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOLD, DIM, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"
WIDTH = 78


@dataclass
class Segment:
    n: int
    title: str
    #: What this segment is meant to prove. Said before it runs, so a viewer knows what to
    #: watch for rather than reconstructing it afterwards.
    claim: str
    commands: list[list[str]] = field(default_factory=list)
    #: What to point at once the output is on screen.
    punchline: str = ""
    #: Segments needing a browser say so, rather than appearing to hang.
    manual: str = ""


SEGMENTS = [
    Segment(
        1,
        "It drives a real browser",
        "A recorded flow replays with no model in the loop. Watch the browser sign in, "
        "search, and open the member record on its own - about 25 seconds.",
        [
            [
                "replay",
                "--capability",
                "member.savings-balance",
                "--input",
                "memberId=100042",
                "--headed",
            ]
        ],
        "Every selector was re-derived at run time from role and accessible name. The app "
        "regenerates each control id per render, so a recorded selector would already be dead.",
    ),
    Segment(
        2,
        "'Not found' is an answer, not a crash",
        "Two lookups that both fail, and must fail differently.",
        [
            ["replay", "--capability", "member.savings-balance", "--input", "memberId=999999"],
            ["replay", "--capability", "member.savings-balance", "--input", "memberId=100777"],
        ],
        "MEMBER_NOT_FOUND and PERMISSION_DENIED are different codes. One means the record "
        "does not exist; the other that it does and this operator may not see it. "
        "Conflating them is a compliance problem, not a tidiness one.",
    ),
    Segment(
        3,
        "An irreversible write needs two approvals",
        "Opening a sub-account is irreversible. Watch the refusals get shorter.",
        [
            [
                "replay",
                "--capability",
                "member.open-subaccount",
                "--input",
                "memberId=100042",
                "--input",
                "nickname=ROOF FUND",
            ],
            ["approve", "member.open-subaccount", "--reviewer", "ops@meridiancu.example"],
            [
                "replay",
                "--capability",
                "member.open-subaccount",
                "--input",
                "memberId=100042",
                "--input",
                "nickname=ROOF FUND",
            ],
            [
                "replay",
                "--capability",
                "member.open-subaccount",
                "--input",
                "memberId=100042",
                "--input",
                "nickname=ROOF FUND",
                "--approve",
            ],
            [
                "replay",
                "--capability",
                "member.open-subaccount",
                "--input",
                "memberId=100042",
                "--input",
                "nickname=ROOF FUND",
                "--approve",
            ],
        ],
        "Blocked needing BOTH gates, then blocked needing only --approve, then success - "
        "and the same command again returns DUPLICATE_RECORD saying nothing was created. "
        "That last sentence is what makes a non-idempotent capability safe to retry.",
    ),
    Segment(
        4,
        "The one case worth pausing on",
        "The confirm commits and then loses its acknowledgement. Nothing on screen "
        "distinguishes that from a write that never landed.",
        [
            [
                "replay",
                "--capability",
                "member.open-subaccount",
                "--input",
                "memberId=100042",
                "--input",
                "nickname=KAYAK FUND",
                "--approve",
                "--fault",
                "write-timeout",
            ],
            [
                "replay",
                "--capability",
                "member.open-subaccount",
                "--input",
                "memberId=100042",
                "--input",
                "nickname=KAYAK FUND",
                "--approve",
            ],
        ],
        "It escalated saying it could not tell - and the second run proves the account HAD "
        "been opened. Reporting APP_ERROR, which is what the screen literally says, would "
        "have invited a retry and given the member two sub-accounts.",
    ),
    Segment(
        5,
        "A human takes over the live session",
        "An undeclared dialog appears. The run parks and hands the SAME browser session to "
        "an operator.",
        [],
        "Same cookies, same auth, same half-filled form - it is the same page object. "
        "While the human holds the lease, automation structurally cannot act.",
        manual=(
            "  1. In ANOTHER terminal, run:\n\n"
            "       uv run cua replay --capability member.savings-balance \\\n"
            "         --input memberId=100042 --fault unknown-dialog --operator-port 4100\n\n"
            "  2. Open  http://localhost:4100/\n"
            "  3. Click 'Take control'\n"
            "  4. Dismiss the dialog ON THE LIVE VIEW\n"
            "  5. Click 'I cleared the obstacle'  -> it re-orients and finishes\n\n"
            "  BONUS, if you have time: click 'I completed the step' WITHOUT dismissing\n"
            "  the dialog first. It refuses and raises a second intervention, because it\n"
            "  re-reads the screen rather than believing you. That came from a real bug."
        ),
    ),
    Segment(
        6,
        "What a calling AI agent sees",
        "The artifact rendered as a tool definition.",
        [["catalog", "--show", "member.open-subaccount"]],
        "The description lists the business outcomes, so an agent knows MEMBER_NOT_FOUND "
        "is a possible ANSWER before it ever invokes - not something to treat as an outage.",
    ),
]


def rule(char: str = "-") -> str:
    return char * WIDTH


def banner(seg: Segment) -> None:
    print(f"\n{BOLD}{rule('=')}")
    print(f"  SEGMENT {seg.n}   {seg.title}")
    print(f"{rule('=')}{RESET}")
    print(f"\n{seg.claim}\n")


def run(args: list[str]) -> None:
    shown = "uv run cua " + " ".join(a if " " not in a else f'"{a}"' for a in args)
    print(f"{GREEN}$ {shown}{RESET}")
    subprocess.run([sys.executable, "-m", "cua.cli", *args], cwd=REPO)
    print()


def reset() -> bool:
    print(f"{BOLD}resetting demo state{RESET}")
    subprocess.run(
        [sys.executable, "-m", "cua.cli", "approve", "member.open-subaccount", "--undo"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    status = json.loads(
        (REPO / "capabilities" / "member.open-subaccount@1.0.0.json").read_text(encoding="utf-8")
    )["status"]
    print(f"  capability -> {status}")
    if status != "draft":
        print(f"{YELLOW}  ! expected draft; segment 3 will show one gate instead of two{RESET}")

    print(f"\n{YELLOW}  The target app must be running, and freshly started.{RESET}")
    print(f"{DIM}  If you have used ROOF FUND or KAYAK FUND on a previous take, restart it:")
    print("      pkill -f 'cua serve-app'   then   uv run cua serve-app")
    print(f"  Otherwise segment 3 opens with DUPLICATE_RECORD instead of success.{RESET}\n")
    return status == "draft"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--auto",
        action="store_true",
        help="Do not wait for a keypress. For a silent recording, where the printed "
        "claim and punchline are the narration.",
    )
    ap.add_argument(
        "--pace",
        type=float,
        default=5.0,
        help="Seconds to hold on each explanation in --auto mode. The default is set to "
        "reading speed, not typing speed: with no voice-over, a viewer has to read the "
        "line before the next command scrolls it away.",
    )
    ap.add_argument("--only", type=int, default=0, help="Rehearse a single segment.")
    ap.add_argument("--no-reset", action="store_true", help="Skip the state reset.")
    args = ap.parse_args()

    print(f"\n{BOLD}Computer-Use Automation - guided demo{RESET}")
    print(
        f"{DIM}An LLM learns a legacy bank UI once; a typed artifact replays it forever.{RESET}\n"
    )

    if not args.no_reset and not args.only:
        reset()

    segments = [s for s in SEGMENTS if not args.only or s.n == args.only]
    for seg in segments:
        banner(seg)
        if not args.auto:
            try:
                input(f"{DIM}  [enter to run]{RESET} ")
            except (EOFError, KeyboardInterrupt):
                print("\nstopped.")
                return 0

        if args.auto:
            # The claim was printed by banner(); hold on it so it can be read before the
            # first command starts scrolling.
            time.sleep(args.pace)

        if seg.manual:
            print(f"{YELLOW}This one is interactive - do it by hand:{RESET}\n")
            print(seg.manual)
            if args.auto:
                print(f"{DIM}  (pausing 30s - do it now, or Ctrl-C and use --only 5){RESET}")
                time.sleep(30)
        else:
            for command in seg.commands:
                run(command)
                if args.auto:
                    time.sleep(args.pace * 0.5)

        print(f"{BOLD}  -> {RESET}{seg.punchline}\n")
        if args.auto:
            time.sleep(args.pace)
        if not args.auto and seg is not segments[-1]:
            try:
                input(f"{DIM}  [enter for the next segment]{RESET} ")
            except (EOFError, KeyboardInterrupt):
                print("\nstopped.")
                return 0

    print(f"\n{rule('=')}")
    print("  Everything above ran with no API key and no model in the loop.")
    print("  Full taxonomy: uv run cua eval   (15 scenarios, ~12 min)")
    print(f"{rule('=')}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
