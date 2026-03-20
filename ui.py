"""
Terminal UI helpers — Rich-based rendering for the agent system.

All display logic lives here. graph.py calls these helpers instead of
using print() directly. This keeps Rich out of the orchestration logic
and makes it easy to iterate on appearance without touching graph code.

Usage:
    from ui import console, agent_header, agent_result, ...

Phase 3 additions:
    - Collapsed lists in packet_tree (count only, no expansion)
    - inspect_repl() — interactive findings/plan drill-down at checkpoints
    - Live status bar (two-thread model) — current agent + elapsed at bottom
    - run_stats_table() — per-agent call count + time at run end
"""

import json
import time
import threading
import queue
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

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
# Global run state (reset per run)
# ─────────────────────────────────────────────

_last_start: Optional[float] = None   # set by agent_header, consumed by agent_result
_run_start: Optional[float] = None    # set by run_start, consumed by run_complete

# Live status bar state — written by background thread, read by main thread for display
_run_status: dict = {
    "current_agent": "",       # name of currently running agent
    "current_emoji": "",       # emoji for the agent
    "current_detail": "",      # extra detail (e.g. "commission 2/4")
    "current_start": None,     # time.monotonic() when it started
    "completed": [],           # list of {name, elapsed, status}
    "phase_count": {},         # {display_name: call_count}
    "task": "",                # human task description (for run log)
    "logs_dir": "./logs",      # where to write run logs
}

# Live display feature flag — set False to revert to Phase 2 behavior (no threading)
LIVE_DISPLAY: bool = True

# Internal live display handle — managed by run_start / run_complete
_live: Optional[Live] = None


# ─────────────────────────────────────────────
# Live status panel renderer
# ─────────────────────────────────────────────

def _make_status_panel() -> Panel:
    """Render the live status panel shown at the bottom during agent runs."""
    agent = _run_status.get("current_agent", "")
    emoji = _run_status.get("current_emoji", "⟳")
    detail = _run_status.get("current_detail", "")
    t0 = _run_status.get("current_start")

    hint = "[dim]explore: findings · plan · flow · queue · f1 · p1 · help[/dim]"
    if agent and t0 is not None:
        elapsed = int(time.monotonic() - t0)
        line1 = f"[blue]{emoji} [bold]{agent}[/bold] · {elapsed}s[/blue]"
        line2 = f"[dim]{detail}[/dim]" if detail else ""
        content = "\n".join(filter(None, [line1, line2, hint]))
    else:
        content = f"[dim]waiting...[/dim]\n{hint}"

    return Panel(content, title="[dim]⟳ Running[/dim]", border_style="dim blue")


# ─────────────────────────────────────────────
# Agent output helpers
# ─────────────────────────────────────────────

def agent_header(emoji: str, name: str, model: str = "", detail: str = "") -> None:
    """Print a styled agent header line, start per-agent timer, update live status."""
    global _last_start
    _last_start = time.monotonic()

    # Update live status bar state
    _run_status["current_agent"] = name
    _run_status["current_emoji"] = emoji
    _run_status["current_detail"] = detail
    _run_status["current_start"] = _last_start

    model_str = f"[dim]/{model}[/dim]" if model else ""
    detail_str = f" [dim]{detail}[/dim]" if detail else ""
    console.print(f"\n{emoji} [bold white]{name}[/bold white]{model_str}{detail_str}")


def agent_result(status: str, summary: str) -> None:
    """Print a color-coded result line with elapsed time, update run stats."""
    global _last_start
    status_lower = (status or "").lower()
    if status_lower in ("complete", "pass", "approved", "synthesized", "pass_with_notes", "healthy"):
        icon, color = "✓", "green"
    elif status_lower in ("fail", "reject", "blocked"):
        icon, color = "✗", "red"
    elif status_lower in ("revise", "needs_human_input", "needs_clarification", "needs_research",
                          "degrading", "critical"):
        icon, color = "⚠", "yellow"
    else:
        icon, color = "·", "dim white"

    elapsed = 0.0
    elapsed_str = ""
    if _last_start is not None:
        elapsed = time.monotonic() - _last_start
        elapsed_str = f" [dim]({elapsed:.1f}s)[/dim]"
        _last_start = None

    console.print(f"   [{color}]{icon}[/{color}] [dim]{status}[/dim] — {summary}{elapsed_str}")

    # Record for run stats table
    agent_name = _run_status.get("current_agent", "?")
    _run_status["completed"].append({"name": agent_name, "elapsed": elapsed, "status": status})
    _run_status["phase_count"][agent_name] = _run_status["phase_count"].get(agent_name, 0) + 1
    _run_status["current_agent"] = ""
    _run_status["current_start"] = None


def log_link(path: str) -> None:
    """Print a dim clickable file:// link to an agent log file."""
    if not path or path.lower() in ("none", "n/a", ""):
        return
    abs_path = str(Path(path).resolve())
    console.print(f"   [dim][link=file://{abs_path}]📄 {path}[/link][/dim]")


def packet_tree(packet: dict) -> None:
    """Rich Tree display of key packet fields. Lists shown as collapsed counts."""
    tree = Tree("[dim]packet[/dim]")
    priority_scalar_keys = ["status", "verdict", "summary", "real_question",
                            "log_ref", "system_health"]
    shown: set = set()

    # Priority scalar fields first
    for key in priority_scalar_keys:
        if key not in packet:
            continue
        val = packet[key]
        if not isinstance(val, list):
            tree.add(f"[dim]{key}:[/dim] {str(val)[:120]}")
            shown.add(key)

    # All list fields collapsed as item count
    for key, val in packet.items():
        if isinstance(val, list):
            n = len(val)
            tree.add(f"[dim]{key} ({n} item{'s' if n != 1 else ''})[/dim]")
            shown.add(key)

    # Remaining non-list fields as "+N more"
    other_keys = [k for k in packet if k not in shown]
    if other_keys:
        tree.add(f"[dim]+{len(other_keys)} more field(s)[/dim]")
    console.print(tree)


def packet_json(packet: dict) -> None:
    """Syntax-highlighted JSON packet dump (full, for debugging)."""
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
# Inspect REPL — drill into findings/plan at checkpoints
# ─────────────────────────────────────────────

_ROUTING_COMMANDS = {"proceed", "pause", "redirect"}
_INSPECT_HINT = (
    "[dim]Inspect: [bold]findings[/bold] · [bold]plan[/bold] · "
    "[bold]f1[/bold]-fN · [bold]p1[/bold]-pN · "
    "[bold]assumptions[/bold] · [bold]notes[/bold] "
    "— or type [bold]proceed[/bold] / [bold]pause[/bold] / [bold]redirect[/bold][/dim]"
)


def _render_item_panel(title: str, item: object, index: int, total: int) -> None:
    """Render a single finding or plan step as a panel."""
    if isinstance(item, dict):
        lines = [f"[dim]{k}:[/dim]  {v}" for k, v in item.items() if v not in (None, "", [])]
    else:
        lines = [str(item)]
    console.print(Panel(
        "\n".join(lines),
        title=f"[bold cyan]{title} {index} of {total}[/bold cyan]",
        border_style="cyan",
        padding=(0, 2),
    ))


def inspect_repl(state_vals: dict) -> str:
    """
    Interactive REPL at a checkpoint. User can inspect findings, plan steps,
    assumptions, and critic notes before issuing a routing command.

    Returns one of: 'proceed', 'pause', 'redirect', or any free-text answer
    (for clarification interrupts).
    """
    findings: list = (state_vals.get("researcher_packet") or {}).get("findings", [])
    plan: list = (state_vals.get("thinker_packet") or {}).get("plan", [])
    assumptions: list = (state_vals.get("thinker_open_assumptions") or [])
    notes: list = (state_vals.get("critic_minor_notes") or [])

    console.print(_INSPECT_HINT)

    while True:
        raw = console.input("[dim]>[/dim] ").strip()
        cmd = raw.lower()

        if not cmd:
            continue

        # ── Routing commands ─────────────────────────────────────────────
        if cmd in _ROUTING_COMMANDS:
            return cmd

        # ── 'findings' — numbered list ───────────────────────────────────
        if cmd == "findings":
            if not findings:
                console.print("   [dim]No findings in current state.[/dim]")
                continue
            console.print()
            for i, f in enumerate(findings, 1):
                claim = f.get("claim", str(f))[:80] if isinstance(f, dict) else str(f)[:80]
                ctype = f.get("claim_type", "") if isinstance(f, dict) else ""
                tag = f"[dim]{ctype}[/dim]  " if ctype else ""
                console.print(f"  [cyan]{i}.[/cyan] {tag}{claim}")
            console.print()
            continue

        # ── 'plan' — numbered list ────────────────────────────────────────
        if cmd == "plan":
            if not plan:
                console.print("   [dim]No plan steps in current state.[/dim]")
                continue
            console.print()
            for i, s in enumerate(plan, 1):
                desc = s.get("description", str(s))[:80] if isinstance(s, dict) else str(s)[:80]
                role = s.get("assigned_to", "") if isinstance(s, dict) else ""
                tag = f"[dim]{role}[/dim]  " if role else ""
                console.print(f"  [magenta]{i}.[/magenta] {tag}{desc}")
            console.print()
            continue

        # ── 'f<N>' — single finding detail ───────────────────────────────
        if cmd.startswith("f") and cmd[1:].isdigit():
            idx = int(cmd[1:])
            if not findings or idx < 1 or idx > len(findings):
                console.print(f"   [dim]No finding {idx}. Use 'findings' to list.[/dim]")
                continue
            _render_item_panel("Finding", findings[idx - 1], idx, len(findings))
            continue

        # ── 'p<N>' — single plan step detail ─────────────────────────────
        if cmd.startswith("p") and cmd[1:].isdigit():
            idx = int(cmd[1:])
            if not plan or idx < 1 or idx > len(plan):
                console.print(f"   [dim]No plan step {idx}. Use 'plan' to list.[/dim]")
                continue
            _render_item_panel("Plan step", plan[idx - 1], idx, len(plan))
            continue

        # ── 'assumptions' ─────────────────────────────────────────────────
        if cmd == "assumptions":
            if not assumptions:
                console.print("   [dim]No open assumptions.[/dim]")
                continue
            console.print()
            for i, a in enumerate(assumptions, 1):
                text = a.get("assumption", str(a)) if isinstance(a, dict) else str(a)
                console.print(f"  [yellow]{i}.[/yellow] {text[:100]}")
            console.print()
            continue

        # ── 'notes' — critic minor notes ──────────────────────────────────
        if cmd == "notes":
            if not notes:
                console.print("   [dim]No critic notes.[/dim]")
                continue
            console.print()
            for i, n in enumerate(notes, 1):
                console.print(f"  [yellow]{i}.[/yellow] {str(n)[:100]}")
            console.print()
            continue

        # ── Unknown — treat as free text (covers clarification answers) ───
        return raw


# ─────────────────────────────────────────────
# Interactive panels
# ─────────────────────────────────────────────

def clarification_panel(question: str) -> None:
    """Render a clarification question as a yellow panel."""
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
    collapsed = []
    i = 0
    while i < len(flow):
        label = flow[i]
        count = 1
        while i + count < len(flow) and flow[i + count] == label:
            count += 1
        color = AGENT_COLORS.get(label, "white")
        tag = f"{label}×{count}" if count > 1 else label
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

def run_start(thread_id: str, task: str, logs_dir: str = "./logs") -> None:
    """Print run header, reset all timing state, optionally start live display."""
    global _last_start, _run_start, _live
    _last_start = None
    _run_start = time.monotonic()
    _run_status["current_agent"] = ""
    _run_status["current_emoji"] = ""
    _run_status["current_detail"] = ""
    _run_status["current_start"] = None
    _run_status["completed"] = []
    _run_status["phase_count"] = {}
    _run_status["task"] = task
    _run_status["logs_dir"] = logs_dir

    console.print()
    console.print(Rule(f"[bold]Run: {thread_id}[/bold]", style="dim"))
    console.print(f"[dim]Task:[/dim] {task}")
    console.print()

    if LIVE_DISPLAY:
        _live = Live(_make_status_panel(), console=console, refresh_per_second=4,
                     transient=True)
        _live.start()


def _stop_live() -> None:
    """Stop the live display if it's running (idempotent)."""
    global _live
    if _live is not None:
        try:
            _live.stop()
        except Exception:
            pass
        _live = None


def live_pause() -> None:
    """Temporarily stop live display for interactive input (checkpoint/clarification)."""
    _stop_live()


def live_resume() -> None:
    """Resume live display after interactive input."""
    global _live
    if LIVE_DISPLAY and _live is None:
        _live = Live(_make_status_panel(), console=console, refresh_per_second=4,
                     transient=True)
        _live.start()


def run_stats_table() -> None:
    """Render a per-agent call count + total time table."""
    completed = _run_status.get("completed", [])
    if not completed:
        return

    # Aggregate by agent name
    agg: dict[str, dict] = {}
    for entry in completed:
        name = entry["name"]
        if name not in agg:
            agg[name] = {"calls": 0, "total": 0.0}
        agg[name]["calls"] += 1
        agg[name]["total"] += entry.get("elapsed", 0.0)

    table = Table(show_header=True, header_style="dim", box=None, padding=(0, 2))
    table.add_column("Agent", style="white")
    table.add_column("Calls", justify="right", style="dim")
    table.add_column("Total Time", justify="right", style="dim")

    grand_total = 0.0
    grand_calls = 0
    for name, data in agg.items():
        t = data["total"]
        grand_total += t
        grand_calls += data["calls"]
        m, s = divmod(int(t), 60)
        t_str = f"{m}m {s}s" if m else f"{t:.1f}s"
        table.add_row(name, str(data["calls"]), t_str)

    # Total row
    m, s = divmod(int(grand_total), 60)
    total_str = f"{m}m {s}s" if m else f"{grand_total:.1f}s"
    table.add_section()
    table.add_row("[dim]Total[/dim]", f"[dim]{grand_calls}[/dim]", f"[dim]{total_str}[/dim]")

    console.print(Rule("[dim]Agent Run Stats[/dim]", style="dim"))
    console.print(table)
    console.print()


def write_run_log(thread_id: str) -> None:
    """Write run stats to {logs_dir}/run_log.jsonl and {logs_dir}/runs/{thread_id}.json."""
    import datetime
    completed = _run_status.get("completed", [])
    logs_dir = Path(_run_status.get("logs_dir", "./logs"))
    runs_dir = logs_dir / "runs"

    # Aggregate by agent name
    agg: dict[str, dict] = {}
    for entry in completed:
        name = entry["name"]
        if name not in agg:
            agg[name] = {"calls": 0, "total_time": 0.0, "statuses": []}
        agg[name]["calls"] += 1
        agg[name]["total_time"] += entry.get("elapsed", 0.0)
        agg[name]["statuses"].append(entry.get("status", ""))

    total_elapsed = 0.0
    if _run_start is not None:
        total_elapsed = time.monotonic() - _run_start

    record = {
        "run_id": thread_id,
        "task": _run_status.get("task", ""),
        "date": datetime.date.today().isoformat(),
        "timestamp": datetime.datetime.now().isoformat(),
        "total_elapsed": round(total_elapsed, 1),
        "agents": [
            {
                "name": name,
                "calls": data["calls"],
                "total_time": round(data["total_time"], 1),
                "statuses": data["statuses"],
            }
            for name, data in agg.items()
        ],
        "completed_steps": len(completed),
    }

    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        runs_dir.mkdir(parents=True, exist_ok=True)

        # Append to rolling JSONL log
        with open(logs_dir / "run_log.jsonl", "a") as f:
            f.write(json.dumps(record) + "\n")

        # Write full run detail
        safe_id = thread_id.replace("/", "_").replace(":", "_")
        with open(runs_dir / f"{safe_id}.json", "w") as f:
            json.dump(record, f, indent=2)
    except Exception as e:
        console.print(f"[dim yellow]⚠ Could not write run log: {e}[/dim yellow]")


def run_complete(thread_id: str) -> None:
    """Stop live display, print stats table, print completion banner, write run log."""
    global _run_start
    _stop_live()
    run_stats_table()
    write_run_log(thread_id)

    time_str = ""
    if _run_start is not None:
        elapsed = time.monotonic() - _run_start
        m, s = divmod(int(elapsed), 60)
        time_str = f" ({m}m {s}s)" if m else f" ({s}s)"
    console.print(Rule(
        f"[bold green]✅  {thread_id} — complete{time_str}[/bold green]",
        style="green",
    ))
    console.print()


# ─────────────────────────────────────────────
# Spinner (used when LIVE_DISPLAY=False or inside nodes)
# ─────────────────────────────────────────────

@contextmanager
def spinner(label: str):
    """Show a spinner while a blocking agent subprocess is running.

    When live display is active, the live panel already shows the current agent
    so this becomes a no-op context manager to avoid nested Rich status conflicts.
    """
    if LIVE_DISPLAY and _live is not None:
        yield  # live panel already shows status — no spinner needed
    else:
        with console.status(f"[blue]{label}[/blue]", spinner="dots"):
            yield
