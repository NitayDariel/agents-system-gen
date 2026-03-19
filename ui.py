"""
Terminal UI helpers — Rich-based rendering for the agent system.

All display logic lives here. graph.py calls these helpers instead of
using print() directly. This keeps Rich out of the orchestration logic
and makes it easy to iterate on appearance without touching graph code.

Usage:
    from ui import console, agent_header, agent_result, ...
"""

import json
from contextlib import contextmanager
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text

# ─────────────────────────────────────────────
# Color palette — one color per agent type
# ─────────────────────────────────────────────

AGENT_COLORS: dict[str, str] = {
    "comm":        "cyan",
    "clarify":     "cyan",
    "thinker":     "magenta",
    "researcher":  "blue",
    "critic":      "yellow",
    "lead_eng":    "orange3",
    "dev":         "green",
    "qa":          "red",
    "integration": "purple",
    "sia":         "dark_orange",
    "checkpoint":  "yellow3",
    "output":      "cyan",
}

# Single shared console — all output goes through this
console = Console(highlight=False)


# ─────────────────────────────────────────────
# Agent output helpers
# ─────────────────────────────────────────────

def agent_header(emoji: str, name: str, model: str = "", detail: str = "") -> None:
    """Print a styled agent header line (replaces the opening print in each node)."""
    model_str = f"[dim]/{model}[/dim]" if model else ""
    detail_str = f" [dim]{detail}[/dim]" if detail else ""
    console.print(f"\n{emoji} [bold white]{name}[/bold white]{model_str}{detail_str}")


def agent_result(status: str, summary: str) -> None:
    """Print a color-coded result line after an agent finishes."""
    status_lower = (status or "").lower()
    if status_lower in ("complete", "pass", "approved", "synthesized", "pass_with_notes"):
        icon, color = "✓", "green"
    elif status_lower in ("fail", "reject", "blocked"):
        icon, color = "✗", "red"
    elif status_lower in ("revise", "needs_human_input", "needs_clarification", "needs_research"):
        icon, color = "⚠", "yellow"
    else:
        icon, color = "·", "dim white"
    console.print(f"   [{color}]{icon}[/{color}] [dim]{status}[/dim] — {summary}")


def packet_json(packet: dict) -> None:
    """Syntax-highlighted JSON packet dump (replaces raw json.dumps print)."""
    syntax = Syntax(
        json.dumps(packet, indent=2),
        "json",
        theme="monokai",
        line_numbers=False,
        word_wrap=True,
    )
    console.print(syntax)


def info(msg: str) -> None:
    """Dim informational line (routing notes, queue counts, etc.)."""
    console.print(f"   [dim]{msg}[/dim]")


def error(msg: str) -> None:
    """Bold red error line."""
    console.print(f"[bold red][ERROR][/bold red] {msg}")


# ─────────────────────────────────────────────
# Interactive panels
# ─────────────────────────────────────────────

def clarification_panel(question: str) -> None:
    """Render a clarification question as a yellow panel, then prompt for input."""
    console.print()
    console.print(Panel(
        f"[bold white]{question}[/bold white]",
        title="[bold yellow]❓  Clarification needed[/bold yellow]",
        border_style="yellow",
        padding=(1, 2),
    ))


def checkpoint_panel(
    stage: str,
    options: Optional[list] = None,
    blockers: Optional[list] = None,
    prompt_text: str = "",
) -> None:
    """Render a human checkpoint as a styled panel."""
    lines = []
    if blockers:
        for b in blockers:
            lines.append(f"  [yellow]•[/yellow] {b}")
        lines.append("")
    opts_str = " [dim]/[/dim] ".join(options or ["proceed", "pause", "redirect"])
    lines.append(f"[dim]Options:[/dim] {opts_str}")
    if prompt_text:
        lines.append(f"\n[dim]{prompt_text}[/dim]")
    console.print()
    console.print(Panel(
        "\n".join(lines),
        title=f"[bold yellow]⏸  {stage}[/bold yellow]",
        border_style="yellow",
        padding=(1, 2),
    ))


# ─────────────────────────────────────────────
# Flow summary
# ─────────────────────────────────────────────

def flow_summary(flow: list) -> None:
    """Render the agent flow chain with per-agent colors at end of run."""
    if not flow:
        return

    # Collapse consecutive duplicates
    collapsed = []
    i = 0
    while i < len(flow):
        label = flow[i]
        count = 1
        while i + count < len(flow) and flow[i + count] == label:
            count += 1
        color = AGENT_COLORS.get(label, "white")
        tag = f"{label}×{count}" if count > 1 else label
        # Escape brackets so Rich doesn't interpret [comm] as a markup tag
        collapsed.append(f"[{color}]\\[{tag}][/{color}]")
        i += count

    chain = " [dim]→[/dim] ".join(collapsed)
    console.print()
    console.print(Rule("[bold green]Agent Flow[/bold green]", style="dim green"))
    console.print(f"  {chain}")
    console.print()


# ─────────────────────────────────────────────
# Run lifecycle
# ─────────────────────────────────────────────

def run_start(thread_id: str, task: str) -> None:
    """Print run header at startup."""
    console.print()
    console.print(Rule(f"[bold]Run: {thread_id}[/bold]", style="dim"))
    console.print(f"[dim]Task:[/dim] {task}")
    console.print()


def run_complete(thread_id: str) -> None:
    """Print run completion banner."""
    console.print()
    console.print(Rule(f"[bold green]✅  {thread_id} — complete[/bold green]", style="green"))
    console.print()


# ─────────────────────────────────────────────
# Spinner
# ─────────────────────────────────────────────

@contextmanager
def spinner(label: str):
    """Show a spinner while a blocking agent subprocess is running."""
    with console.status(f"[blue]{label}[/blue]", spinner="dots"):
        yield
