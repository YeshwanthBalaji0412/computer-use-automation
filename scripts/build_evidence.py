"""Regenerate everything under evidence/demo/.

Committed evidence has to be reproducible, or a reviewer cannot tell curated output from
a screenshot of a good day. Running this script from a clean checkout produces the
directory byte-for-byte apart from run ids and timestamps.

    uv run cua serve-app                 # terminal 1
    uv run python scripts/build_evidence.py

No API key required: discovery runs from the recorded transcript, and replay never calls
a model at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_secrets

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "evidence" / "demo"
RUNS = REPO / "evidence" / "runs"


def run(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "cua.cli", *args], cwd=REPO, capture_output=True, text=True
    )
    return result.stdout + result.stderr


def newest_run(prefix: str) -> Path:
    candidates = sorted(RUNS.glob(f"{prefix}_*"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise SystemExit(f"no {prefix} run found - is the target app running?")
    return candidates[-1]


def capture(prefix: str, name: str, console: str) -> Path:
    """Copy a run directory into evidence/demo/ alongside the console output."""
    source = newest_run(prefix)
    target = DEMO / name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    (target / "console.txt").write_text(console, encoding="utf-8")
    print(f"  {name:<26} <- {source.name}")
    return target


async def escalation_demo() -> str:
    """A replay that parks, an operator who resolves it, and the run finishing.

    Scripted rather than performed by hand so the evidence is reproducible; it drives the
    same console HTTP API the browser page uses, so nothing here is a shortcut the real
    operator surface does not also take.
    """
    import socket

    import httpx

    import cua.surface.web_surface as ws
    from cua.app.replay import run_replay

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    live: list[object] = []
    original = ws.WebSurface.launch

    async def capture_page(**kwargs):  # type: ignore[no-untyped-def]
        surface, pw, browser = await original(**kwargs)
        live.clear()
        live.append(surface.page)
        return surface, pw, browser

    ws.WebSurface.launch = staticmethod(capture_page)  # type: ignore[assignment]
    log: list[str] = []

    async def operator(task: asyncio.Task[object]) -> None:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20) as client:
            while not task.done():
                try:
                    state = (await client.get("/api/state")).json()
                except httpx.HTTPError:
                    await asyncio.sleep(0.2)
                    continue
                pending = [i for i in state["interventions"] if i["status"] in ("open", "taken")]
                if state["awaiting_human"] and pending:
                    item = pending[0]
                    log.append(f"intervention  {item['reason']} at {item['step_id']}")
                    log.append(f"  why         {item['explain']}")
                    log.append(f"  lease       {state['owner']} (epoch {state['epoch']})")
                    await client.post(f"/api/interventions/{item['id']}/take")

                    page = live[0]
                    for frame in page.frames:  # type: ignore[attr-defined]
                        button = frame.get_by_role("button", name="Acknowledge")
                        if await button.count():
                            await button.first.click()
                            log.append("  operator    dismissed the hold notice")
                            break

                    for _ in range(25):
                        after = (await client.get("/api/state")).json()
                        if after["human_actions"]:
                            log.append(f"  recorded    {after['human_actions']}")
                            break
                        await asyncio.sleep(0.1)

                    await client.post(
                        f"/api/interventions/{item['id']}/resume",
                        json={"disposition": "recovered", "note": "dismissed the notice"},
                    )
                    log.append("  resumed     as 'recovered'")
                await asyncio.sleep(0.2)

    try:
        task: asyncio.Task[object] = asyncio.create_task(
            run_replay(
                capability_name="member.savings-balance",
                inputs={"memberId": "100042"},
                operator_port=port,
                fault="unknown-dialog",
                goal="Look up member 100042 and read their current savings balance",
            )
        )
        driver = asyncio.create_task(operator(task))
        result = await asyncio.wait_for(task, timeout=180)
        driver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await driver
    finally:
        ws.WebSurface.launch = original  # type: ignore[assignment]

    from cua.app.replay import render

    return "\n".join(log) + "\n\n" + render(result)


WRITE = ("replay", "--capability", "member.open-subaccount")
NICKNAME = "ROOF FUND"


def write_flow() -> None:
    """The two-gate chain, in the order a reviewer should read it.

    Sequential and stateful on purpose. The duplicate refusal only means something
    *after* the account has been opened, so these runs are ordered rather than
    independent - which is exactly why they are here and not in the eval matrix, where
    every row has to be re-runnable.

    Starts by withdrawing the approval so the first run shows *both* gates missing. The
    committed artifact ships approved; this is the only way to demonstrate the state it
    was in before a human looked at it.
    """
    print("write flow: blocked -> approved -> success -> duplicate -> ambiguous")
    lines = [run("approve", "member.open-subaccount", "--undo")]

    lines.append(run(*WRITE, "--input", "memberId=100042", "--input", f"nickname={NICKNAME}"))
    capture("rep", "write-1-blocked-unapproved", "\n".join(lines[-1:]))

    approved = run("approve", "member.open-subaccount", "--reviewer", "ops@meridiancu.example")
    print(f"  {approved.strip().splitlines()[0]}")

    console = run(*WRITE, "--input", "memberId=100042", "--input", f"nickname={NICKNAME}")
    capture("rep", "write-2-blocked-no-caller-approval", approved + "\n" + console)

    console = run(
        *WRITE, "--input", "memberId=100042", "--input", f"nickname={NICKNAME}", "--approve"
    )
    capture("rep", "write-3-success", console)

    console = run(
        *WRITE, "--input", "memberId=100042", "--input", f"nickname={NICKNAME}", "--approve"
    )
    capture("rep", "write-4-duplicate", console)

    console = run(
        *WRITE,
        "--input",
        "memberId=100042",
        "--input",
        "nickname=KAYAK FUND",
        "--approve",
        "--fault",
        "write-timeout",
    )
    capture("rep", "write-5-ambiguous-outcome", console)


#: Runs whose trace is worth ~1MB in a repository a reviewer clones. A trace answers
#: "what did the browser actually do", which is a question you only ask when perception
#: and reality disagreed - so the failures, the escalations, the cross-tenant run, and one
#: recording keep theirs. A trace of a run that did exactly what it was supposed to
#: demonstrates only that traces exist, which the four below already do.
TRACE_KEEP = {
    "discovery",
    "replay-app-error",
    "replay-escalated-handoff",
    "replay-lakeside",
    "write-5-ambiguous-outcome",
}


def prune_traces() -> None:
    freed = 0
    for trace in DEMO.rglob("trace.zip"):
        if trace.parent.name not in TRACE_KEEP:
            freed += trace.stat().st_size
            trace.unlink()
    kept = sum(t.stat().st_size for t in DEMO.rglob("trace.zip"))
    print(
        f"\ntraces: kept {len(list(DEMO.rglob('trace.zip')))} ({kept / 1e6:.1f} MB), "
        f"dropped {freed / 1e6:.1f} MB from runs that went exactly as intended"
    )


def main() -> None:
    DEMO.mkdir(parents=True, exist_ok=True)
    print("discovery (from the recorded transcript, no API key)")
    capture("dis", "discovery", run("discover", "--mock"))

    print("replay")
    for name, args in [
        ("replay-success", ["--input", "memberId=100042"]),
        ("replay-not-found", ["--input", "memberId=999999"]),
        ("replay-permission-denied", ["--input", "memberId=100777"]),
        ("replay-app-error", ["--input", "memberId=100042", "--fault", "500"]),
        ("replay-slow-recovered", ["--input", "memberId=100042", "--fault", "slow"]),
        ("replay-lakeside", ["--input", "memberId=100042", "--tenant", "lakeside"]),
    ]:
        console = run("replay", "--capability", "member.savings-balance", *args)
        capture("rep", name, console)

    print("escalation and handoff")
    console = asyncio.run(escalation_demo())
    capture("rep", "replay-escalated-handoff", console)

    print("\nattended discovery of the write flow (authorised by a scripted operator)")
    recorded = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "record_flow_b.py")],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if recorded.returncode != 0:
        raise SystemExit(f"could not record the write flow:\n{recorded.stdout}{recorded.stderr}")
    capture("dis", "discovery-write-flow", recorded.stdout + recorded.stderr)

    write_flow()

    print("matrices")
    (DEMO / "eval-matrix.txt").write_text(run("eval"), encoding="utf-8")
    (DEMO / "conformance-sweep.txt").write_text(
        run("verify", "--capability", "member.savings-balance"), encoding="utf-8"
    )
    for name in ("member.savings-balance@1.0.0", "member.open-subaccount@1.0.0"):
        shutil.copy(REPO / "capabilities" / f"{name}.json", DEMO / f"{name}.json")
    (DEMO / "catalog.txt").write_text(run("catalog"), encoding="utf-8")

    prune_traces()

    print("\nredaction gate")
    if check_secrets.main() != 0:
        raise SystemExit("refusing to leave leaked evidence on disk")
    print(f"evidence written to {DEMO.relative_to(REPO)}")


if __name__ == "__main__":
    main()
