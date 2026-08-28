"""`cua observe` - render an Observation for a human.

Kept out of cli.py so the CLI stays a thin composition root, and out of web_surface.py
so the adapter has no presentation concerns.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

from cua.surface.base import Action, ActionType, ElementNode, Observation
from cua.surface.web_surface import WebSurface


async def run_observe(
    url: str,
    *,
    login: bool = True,
    aria: bool = False,
    json_out: bool = False,
    headed: bool = False,
) -> None:
    surface, pw, browser = await WebSurface.launch(headed=headed)
    try:
        if login:
            await _sign_in(surface, url)
        await surface.act(Action(type=ActionType.NAVIGATE, url=url))
        obs = await surface.observe()

        if json_out:
            print(obs.model_dump_json(indent=2))
        else:
            print(render(obs, show_aria=aria))
    finally:
        await surface.close()
        await browser.close()
        await pw.stop()


async def _sign_in(surface: WebSurface, url: str) -> None:
    """Sign into the demo app so content frames are reachable.

    Credentials are demo values for a fake local app. They are still passed as values
    here rather than recorded anywhere - nothing in this path writes to disk.
    """
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0] != "tenants":
        return
    root = urlunparse((parsed.scheme, parsed.netloc, f"/tenants/{parts[1]}/", "", "", ""))

    await surface.act(Action(type=ActionType.NAVIGATE, url=root))
    obs = await surface.observe()
    user = _find(obs, "textbox", "User ID")
    pwd = _find(obs, "textbox", "Password")
    btn = _find(obs, "button", "Sign In")
    if not (user and pwd and btn):
        return
    await surface.act(Action(type=ActionType.FILL, ref=user.ref, text="operator"))
    await surface.act(Action(type=ActionType.FILL, ref=pwd.ref, text="demo-pass"))
    await surface.act(Action(type=ActionType.CLICK, ref=btn.ref))


def _find(obs: Observation, role: str, name: str) -> ElementNode | None:
    return next(
        (e for e in obs.elements if e.role == role and e.name.lower() == name.lower()),
        None,
    )


def render(obs: Observation, *, show_aria: bool = False) -> str:
    lines: list[str] = [
        f"url          {obs.url}",
        f"title        {obs.title}",
        f"frames       {[('/'.join(p) or 'main') for p in obs.frame_paths]}",
        f"fingerprint  {obs.fingerprint}",
        f"elements     {len(obs.elements)}" + ("  (truncated)" if obs.truncated else ""),
        "",
    ]

    by_frame: dict[str, list[ElementNode]] = {}
    for node in obs.elements:
        by_frame.setdefault("/".join(node.frame_path) or "main", []).append(node)

    for frame, nodes in by_frame.items():
        lines.append(f"[frame: {frame}]")
        section = object()
        for node in nodes:
            if node.section != section:
                section = node.section
                lines.append(f"  ~ {node.section or '(no section)'}")
            lines.append(f"    {node.ref:<5} {_one_line(node)}")
        lines.append("")

    if show_aria:
        lines.append("--- aria snapshot (what the model reads) ---")
        lines.append(obs.aria_yaml)

    return "\n".join(lines)


def _one_line(node: ElementNode) -> str:
    out = f"{node.role:<13} {node.name!r}"
    if node.value:
        out += f" = {node.value!r}"
    if node.states:
        out += f" [{','.join(node.states)}]"
    if node.row_context and node.row_context.row_key:
        key = ", ".join(f"{k}={v!r}" for k, v in list(node.row_context.row_key.items())[:2])
        out += f"  <row {key}"
        if node.row_context.column_header:
            out += f" | col {node.row_context.column_header!r}"
        out += ">"
    return out
