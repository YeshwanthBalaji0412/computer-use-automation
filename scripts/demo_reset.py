"""Put the demo into a known state, so a recording can be retaken without surprises.

Two things drift between runs and both break the interesting demos silently:

* `member.open-subaccount` ends up `approved`, so the first replay shows only one missing
  gate instead of two - and the two-gate chain is the whole point of that segment;
* the target app accumulates sub-accounts in memory, so a nickname that opened cleanly
  last time returns `DUPLICATE_RECORD` on the retake. There is no reset endpoint, on
  purpose: a real core banking system does not have one. Restarting the process is the
  honest equivalent.

Run this, then follow the storyboard printed at the end.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    print("resetting demo state\n")

    # 1. The capability back to draft, so the first replay shows *both* gates missing.
    result = subprocess.run(
        [sys.executable, "-m", "cua.cli", "approve", "member.open-subaccount", "--undo"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    print(
        f"  capability -> draft   {result.stdout.strip().splitlines()[0] if result.stdout else ''}"
    )

    status = json.loads(
        (REPO / "capabilities" / "member.open-subaccount@1.0.0.json").read_text(encoding="utf-8")
    )["status"]
    if status != "draft":
        print(f"  ! expected draft, got {status}")
        return 1

    # 2. A fresh app process, so no nickname has been used yet.
    subprocess.run(["pkill", "-f", "cua serve-app"], capture_output=True)
    time.sleep(2)
    print("  target app -> stopped (restart it in terminal 1)")

    print("\n" + "=" * 68)
    print("NOW: start the app in terminal 1, then follow the storyboard\n")
    print("  terminal 1:  uv run cua serve-app")
    print("  terminal 2:  the commands below, in order\n")
    print("=" * 68)
    print(STORYBOARD)
    return 0


STORYBOARD = """
--- 1. it drives a real browser (~25s) -------------------------------------
uv run cua replay --capability member.savings-balance --input memberId=100042 --headed

    A browser opens and works through sign-in, search, and the member record on
    its own. Nothing is scripted against ids: the app regenerates every control
    id on each render.

--- 2. "not found" is an answer, not a crash (~15s) ------------------------
uv run cua replay --capability member.savings-balance --input memberId=999999

    business_outcome / MEMBER_NOT_FOUND, not a failure.

--- 3. an irreversible write needs two approvals (~45s) --------------------
uv run cua replay --capability member.open-subaccount \\
  --input memberId=100042 --input nickname="ROOF FUND"
    -> blocked   needs: capability.status == approved, --approve

uv run cua approve member.open-subaccount --reviewer ops@meridiancu.example

uv run cua replay --capability member.open-subaccount \\
  --input memberId=100042 --input nickname="ROOF FUND"
    -> blocked   needs: --approve            <- one gate fell away

uv run cua replay --capability member.open-subaccount \\
  --input memberId=100042 --input nickname="ROOF FUND" --approve
    -> success   confirmationNumber

--- 4. the case worth pausing on (~40s) ------------------------------------
uv run cua replay --capability member.open-subaccount \\
  --input memberId=100042 --input nickname="KAYAK FUND" --approve --fault write-timeout
    -> escalated / ambiguous_write_outcome

uv run cua replay --capability member.open-subaccount \\
  --input memberId=100042 --input nickname="KAYAK FUND" --approve
    -> DUPLICATE_RECORD                      <- it HAD been opened

    The write committed and only the acknowledgement was lost. Reporting
    APP_ERROR would have invited a retry and given the member two accounts.

--- 5. a human takes over the live session (~50s) --------------------------
uv run cua replay --capability member.savings-balance --input memberId=100042 \\
  --fault unknown-dialog --operator-port 4100

    The run parks. Open http://localhost:4100/ -> Take control ->
    dismiss the dialog on the live view -> I cleared the obstacle.
    It re-reads the screen and finishes. Same browser session throughout.
"""


if __name__ == "__main__":
    raise SystemExit(main())
