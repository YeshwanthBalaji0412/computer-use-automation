"""Composition root.

This is the only module allowed to wire concrete adapters (Playwright, the Anthropic
client) into the engines. Everything downstream depends on interfaces, which is what keeps
the import-linter contracts in setup.cfg satisfiable.
"""

from __future__ import annotations

from typing import Annotated

import typer

app = typer.Typer(
    name="cua",
    help="Computer-use automation: an LLM discovers a legacy UI flow once; "
    "deterministic replay executes it forever.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command("serve-app")
def serve_app(
    port: Annotated[int, typer.Option(help="Port for the target app.")] = 4000,
    reload: Annotated[bool, typer.Option(help="Auto-reload on edit.")] = False,
) -> None:
    """Run the CoreLink Servicing Console — the deliberately hostile legacy target app."""
    import uvicorn

    typer.echo(f"CoreLink Servicing Console -> http://localhost:{port}/tenants/meridian")
    typer.echo(f"                             http://localhost:{port}/tenants/lakeside")
    uvicorn.run("targetapp.app:app", host="127.0.0.1", port=port, reload=reload)


@app.command()
def observe(
    url: Annotated[str, typer.Option(help="URL to observe.")],
    login: Annotated[
        bool, typer.Option(help="Sign in first, so content frames are reachable.")
    ] = True,
    aria: Annotated[bool, typer.Option(help="Also print the raw aria snapshot.")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Emit the Observation as JSON.")] = False,
    headed: Annotated[bool, typer.Option(help="Show the browser.")] = False,
) -> None:
    """Print the semantic observation (roles + accessible names) for a page.

    The workhorse debugging tool for the perception layer: it shows exactly what the
    model will see, which is roles and accessible names - never HTML, never selectors.
    """
    import asyncio

    from cua.app.observe import run_observe

    asyncio.run(run_observe(url, login=login, aria=aria, json_out=json_out, headed=headed))


@app.command()
def discover(
    goal: Annotated[str, typer.Option(help="Natural-language goal.")] = "",
    target: Annotated[str, typer.Option(help="Entry-point URL.")] = "",
    tenant: Annotated[str, typer.Option(help="Tenant this run is recorded against.")] = "meridian",
    mock: Annotated[
        bool, typer.Option("--mock", help="Replay a recorded LLM transcript. No API key needed.")
    ] = False,
    fixture: Annotated[str, typer.Option(help="Transcript to replay with --mock.")] = "",
    headed: Annotated[bool, typer.Option(help="Show the browser.")] = False,
) -> None:
    """Run the LLM observe->decide->act loop and compile a capability artifact.

    Only this command needs an API key. Replay, the eval matrix, and `--mock` do not.
    """
    import asyncio
    from pathlib import Path

    from cua.app.discover import run_discover

    if not mock and not (goal and target):
        typer.echo("--goal and --target are required (or use --mock)")
        raise typer.Exit(2)

    code = asyncio.run(
        run_discover(
            goal=goal or "replay a recorded discovery transcript",
            target=target or "http://localhost:4000/tenants/meridian/",
            tenant=tenant,
            mock=mock,
            headed=headed,
            fixture=Path(fixture) if fixture else None,
        )
    )
    raise typer.Exit(code)


@app.command()
def replay(
    capability: Annotated[str, typer.Option(help="e.g. member.savings-balance@1.0.0")],
    # Named `input` to match the brief's vocabulary ("typed input parameters").
    input: Annotated[  # noqa: A002
        list[str] | None, typer.Option(help="Repeatable key=value input parameter.")
    ] = None,
    tenant: Annotated[str, typer.Option(help="Tenant overlay to apply.")] = "meridian",
    approve: Annotated[
        bool, typer.Option("--approve", help="Caller approval for irreversible actions.")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Emit ReplayResult as JSON.")] = False,
    fault: Annotated[
        str,
        typer.Option(
            help="Inject a runtime fault: slow|interstitial|expire|500|"
            "unknown-dialog|write-timeout."
        ),
    ] = "",
    operator_port: Annotated[
        int,
        typer.Option(
            help="Serve the operator console on this port and hand off to a human when "
            "the run gets stuck. Omit to run unattended."
        ),
    ] = 0,
    headed: Annotated[bool, typer.Option(help="Show the browser window.")] = False,
) -> None:
    """Deterministically replay a capability. No LLM is involved in any decision.

    Unattended by default. With --operator-port, an escalation parks the run and opens
    the live session to a human instead of terminating it; the two modes share one code
    path and differ only in whether there is anyone to cede control to.
    """
    import asyncio

    from cua.app.replay import exit_code, parse_inputs, render, run_replay

    try:
        values = parse_inputs(input or [])
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(2) from exc

    result = asyncio.run(
        run_replay(
            capability_name=capability,
            inputs=values,
            tenant_id=tenant,
            approve=approve,
            fault=fault or None,
            headed=headed,
            operator_port=operator_port or None,
        )
    )
    typer.echo(result.model_dump_json(indent=2) if json_out else render(result))
    raise typer.Exit(exit_code(result))


@app.command()
def verify(
    capability: Annotated[str, typer.Option(help="Capability to smoke-test.")],
    tenants: Annotated[str, typer.Option(help="'all' or a comma-separated list.")] = "all",
) -> None:
    """Read-only conformance sweep across tenants: resolve locators, assert preconditions."""
    raise NotImplementedError("phase 6")


@app.command()
def catalog(
    show: Annotated[str, typer.Option(help="Capability id to show in detail.")] = "",
) -> None:
    """List saved capabilities as the callable tool contract an AI agent would see."""
    raise NotImplementedError("phase 6")


@app.command()
def agent(
    ask: Annotated[str, typer.Argument(help="Natural-language request.")],
) -> None:
    """Demo: an LLM agent picks a capability from the catalog and invokes it by name."""
    raise NotImplementedError("phase 6")


@app.command("eval")
def eval_(
    scenario: Annotated[str, typer.Option(help="Run one scenario instead of the matrix.")] = "",
) -> None:
    """Run the scenario matrix (happy path, business outcomes, recoveries, hard failures).

    Needs no API key: replay never calls a model.
    """
    import asyncio

    from cua.app.evaluate import run_matrix

    matrix = asyncio.run(run_matrix(only=scenario))
    typer.echo(matrix.render())
    raise typer.Exit(0 if matrix.passed else 1)


if __name__ == "__main__":
    app()
