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

    print("matrices")
    (DEMO / "eval-matrix.txt").write_text(run("eval"), encoding="utf-8")
    (DEMO / "conformance-sweep.txt").write_text(
        run("verify", "--capability", "member.savings-balance"), encoding="utf-8"
    )
    shutil.copy(
        REPO / "capabilities" / "member.savings-balance@1.0.0.json",
        DEMO / "member.savings-balance@1.0.0.json",
    )

    scanned = 0
    for path in DEMO.rglob("*"):
        if path.suffix in (".json", ".jsonl", ".md", ".txt"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for secret in ("demo-pass", "4,182.55"):
                if secret in text:
                    raise SystemExit(f"regulated value {secret!r} leaked into {path}")
            scanned += 1
    print(f"\nscanned {scanned} committed files for leaked values: clean")
    print(f"evidence written to {DEMO.relative_to(REPO)}")


if __name__ == "__main__":
    main()
