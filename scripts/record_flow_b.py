"""Record the write flow: open a member sub-account.

    uv run cua serve-app                              # terminal 1
    uv run python scripts/record_flow_b.py            # no API key
    uv run python scripts/record_flow_b.py --live     # a real model, ~$0.30

A write flow cannot be recorded the way a read flow can. Policy escalates every
irreversible action *during* discovery - the model is by definition working out a UI it
does not yet understand, which is the worst possible moment to let it press "Confirm and
Open Account" - so the run parks and waits for a human to authorise that one action.

This script is that human, scripted. It drives the operator console over the same HTTP API
the browser page uses, so nothing here is a shortcut the real surface does not also take:
poll for an open intervention, take the lease, approve, hand back. Doing it by hand is the
demo; doing it here is what makes the artifact reproducible from a clean checkout.

`--live` swaps the model and changes nothing else. That is the point of the flag: the
authorisation path, the recorder and the compiler are identical, so a live run is a
question about the *model*, not about this machinery.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

FIXTURE = REPO / "evidence" / "fixtures" / "open-subaccount-transcript.json"

GOAL = (
    "Open a new SAVINGS sub-account nicknamed HOLIDAY FUND for member 100042, "
    "and read back the confirmation number"
)


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


async def authorise(port: int, task: asyncio.Task[Any], log: list[str]) -> int:
    """Stand in for the operator at the console.

    Approves any irreversible action the model proposes, which is the right behaviour for
    a *recording* session and emphatically not for replay: the artifact this produces
    still comes out `status: draft`, and still needs `cua approve` plus a per-invocation
    `--approve` before replay will run it. Authorising the recording and authorising the
    execution are two different decisions, made by two different people, at two different
    times - which is the whole reason `risk_class` and `status` are separate fields.
    """
    granted = 0
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
        while not task.done():
            try:
                state = (await client.get("/api/state")).json()
            except httpx.HTTPError:
                await asyncio.sleep(0.2)
                continue

            pending = [i for i in state["interventions"] if i["status"] in ("open", "taken")]
            if state["awaiting_human"] and pending:
                item = pending[0]
                granted += 1
                log.append(f"authorisation #{granted}  {item['reason']}")
                log.append(f"  asked       {item['explain']}")
                log.append(f"  lease       {state['owner']} (epoch {state['epoch']})")
                await client.post(f"/api/interventions/{item['id']}/take")
                await client.post(
                    f"/api/interventions/{item['id']}/resume",
                    json={
                        "disposition": "step_completed",
                        "note": "authorised for this recording session",
                    },
                )
                log.append("  operator    approved; automation performs its own step")
            await asyncio.sleep(0.2)
    return granted


async def record(*, live: bool, headed: bool) -> int:
    from cua.app.discover import run_discover

    port = free_port()
    log: list[str] = []

    if not live:
        # The scripted plan lives with the tests that assert on it, so there is one copy
        # rather than two that drift. It resolves elements from the rendered observation
        # by role and accessible name - the same identity the locator ladder uses - so it
        # is honest about the one thing a stand-in could otherwise fake.
        from tests.integration.test_discovery import ScriptedLLM
        from tests.integration.test_write_flow import open_subaccount_plan

        import cua.app.discover as discover_module

        scripted = ScriptedLLM(open_subaccount_plan())
        original = discover_module.OpenAIClient
        discover_module.OpenAIClient = lambda: scripted  # type: ignore[assignment,misc,return-value]
    else:
        original = None

    try:
        task: asyncio.Task[int] = asyncio.create_task(
            run_discover(
                goal=GOAL,
                target="http://localhost:4000/tenants/meridian/",
                tenant="meridian",
                mock=False,
                headed=headed,
                operator_port=port,
            )
        )
        driver = asyncio.create_task(authorise(port, task, log))
        code = await asyncio.wait_for(task, timeout=600)
        granted = await driver
    finally:
        if original is not None:
            import cua.app.discover as discover_module

            discover_module.OpenAIClient = original  # type: ignore[assignment]

    print()
    print("\n".join(log))
    print(f"\n{granted} irreversible action(s) authorised by a human")

    if code != 0:
        print("\nno artifact written - the run did not finish")
        return code

    newest = max(
        (REPO / "evidence" / "runs").glob("dis_*"), key=lambda p: p.stat().st_mtime, default=None
    )
    if newest is not None and (newest / "transcript.json").exists():
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(newest / "transcript.json", FIXTURE)
        print(f"transcript -> {FIXTURE.relative_to(REPO)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use a real model instead of the scripted plan. Needs OPENAI_API_KEY.",
    )
    parser.add_argument("--headed", action="store_true", help="Show the browser.")
    args = parser.parse_args()
    return asyncio.run(record(live=args.live, headed=args.headed))


if __name__ == "__main__":
    raise SystemExit(main())
