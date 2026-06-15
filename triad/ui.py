"""Terminal rendering with `rich`.

Two display primitives:
  * live_parallel — N agents stream simultaneously into side-by-side panels.
  * live_single   — one agent streams into a full-width panel (used for relay/council steps).
"""
from __future__ import annotations

import asyncio
import textwrap
from typing import Callable, Dict, List

from rich.columns import Columns
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text


def _fit(text: str, width: int, height: int) -> str:
    """Wrap to `width` and keep only the last `height` rows (so the latest text stays visible)."""
    rows: List[str] = []
    for line in text.split("\n"):
        if line == "":
            rows.append("")
        else:
            rows.extend(textwrap.wrap(line, width=max(8, width)) or [""])
    if len(rows) > height:
        rows = rows[-height:]
    return "\n".join(rows)


def _columns(console: Console, agents, buffers: Dict[str, str], statuses: Dict[str, str]):
    n = max(1, len(agents))
    col_w = max(26, console.width // n - 1)
    inner_w = col_w - 4
    height = max(8, console.height - 8)
    panels = []
    for a in agents:
        body = _fit(buffers.get(a.name, ""), inner_w, height)
        panels.append(
            Panel(
                Text(body),
                title=f"[bold]{a.label}[/bold]",
                subtitle=statuses.get(a.name, ""),
                border_style=a.color,
                width=col_w,
                height=height + 2,
                padding=(0, 1),
            )
        )
    return Columns(panels, equal=True, expand=True)


async def live_parallel(
    console: Console,
    agents,
    stream_factory: Callable,  # stream_factory(agent) -> async iterator of text deltas
) -> Dict[str, str]:
    """Run every agent's stream concurrently, rendering all of them live."""
    buffers: Dict[str, str] = {a.name: "" for a in agents}
    statuses: Dict[str, str] = {a.name: "[dim]● thinking…[/dim]" for a in agents}

    async def run(agent):
        try:
            async for delta in stream_factory(agent):
                buffers[agent.name] += delta
            statuses[agent.name] = "[green]✓ done[/green]"
        except Exception as e:  # one agent failing shouldn't kill the others
            statuses[agent.name] = "[red]✗ error[/red]"
            buffers[agent.name] += f"\n\n[error] {type(e).__name__}: {e}"

    tasks = [asyncio.create_task(run(a)) for a in agents]
    try:
        with Live(_columns(console, agents, buffers, statuses), console=console,
                  refresh_per_second=12, screen=False) as live:
            while not all(t.done() for t in tasks):
                live.update(_columns(console, agents, buffers, statuses))
                await asyncio.sleep(0.08)
            live.update(_columns(console, agents, buffers, statuses))
    finally:
        # On Ctrl-C / error, don't leave provider streams running in the background.
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return dict(buffers)


async def live_single(console: Console, agent, stream_iter, title: str) -> str:
    """Stream a single agent into one full-width panel."""
    buffer = ""
    status = "[dim]● thinking…[/dim]"

    def render():
        height = max(6, console.height - 8)
        body = _fit(buffer, console.width - 4, height)
        return Panel(Text(body), title=f"[bold]{title}[/bold]", subtitle=status,
                     border_style=agent.color, padding=(0, 1))

    with Live(render(), console=console, refresh_per_second=12, screen=False) as live:
        try:
            async for delta in stream_iter:
                buffer += delta
                live.update(render())
            status = "[green]✓ done[/green]"
        except Exception as e:
            status = "[red]✗ error[/red]"
            buffer += f"\n\n[error] {type(e).__name__}: {e}"
        live.update(render())
    return buffer
