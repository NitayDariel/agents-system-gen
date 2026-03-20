# Phase 3 — Interactive Terminal UX

**Status:** Planning → Implementation
**Files touched:** `ui.py`, `graph.py`
**Goal:** Live status bar during agent runs, collapsed list display, inspect REPL at checkpoints, run stats table.

---

## Architecture: Threading Model for Parallel Agent + Interactive Terminal

### The Problem
`app.invoke()` is synchronous and fully blocks the main thread. Every `claude -p` subprocess inside it also blocks. Without threads, the terminal is frozen while agents run — no live updates, no keyboard input possible.

### The Solution: Two-Thread Model

```
┌─────────────────────────────────────────────────────┐
│  MAIN THREAD                                        │
│                                                     │
│  rich.live.Live(layout)  ← refreshes @ 4Hz         │
│    └─ status panel (bottom)  ← reads _run_status   │
│    └─ console.print() scrolls above (existing)     │
│                                                     │
│  At interrupt:                                      │
│    live.stop()  ← freeze display                   │
│    render checkpoint/clarification panel            │
│    inspect REPL loop (user types f1, p2, proceed)  │
│    live.start() ← resume display                   │
└─────────────────────────────────────────────────────┘
          ↕  queue.Queue + threading.Event
┌─────────────────────────────────────────────────────┐
│  BACKGROUND THREAD                                  │
│                                                     │
│  _graph_worker():                                   │
│    while True:                                      │
│      app.invoke(cmd, config)                        │
│      if snapshot.next → signal interrupt            │
│        event_queue.put(interrupt_data)              │
│        response = response_queue.get()  ← blocks   │
│        cmd = Command(resume=response)               │
│      else → signal complete, exit                   │
└─────────────────────────────────────────────────────┘
```

### Shared State: `ui._run_status`
A module-level dict in `ui.py` written by the background thread (via `agent_header` / `agent_result`), read by the main thread's Live renderer. No locks needed: Python GIL protects simple dict writes; display reads are safe for eventual consistency.

```python
_run_status = {
    "thread_id": "",
    "current_agent": "",          # name of currently running agent
    "current_start": None,        # time.monotonic() when it started
    "completed": [],              # list of {name, status, elapsed}
    "phase_count": {},            # {agent_label: call_count}
}
```

### Interrupt Handoff Protocol
```
Background: app.invoke() hits interrupt() → returns with snapshot.next set
Background: writes interrupt payload to event_queue
Background: blocks on response_queue.get()

Main: detects event_queue has item → live.stop()
Main: renders panel / runs inspect REPL
Main: puts user response into response_queue
Main: live.start()

Background: unblocks, Command(resume=response), continues
```

---

## Feature 3a — Collapsed Lists in packet_tree

**Problem:** `packet_tree` currently expands list values up to 5 items. For researcher findings (6+) or plan steps (4+), this is noisy in the scrolling log.

**Fix:** Lists always render as `findings (6 items)` — collapsed. Only the count is shown inline, never the items.

**Before:**
```
packet
├── findings
│   ├── {claim_type: fact, text: S3 default...}
│   ├── {claim_type: gap, text: No KMS...}
│   └── … +4 more
```

**After:**
```
packet
├── findings (6 items)
├── key_outputs (1 item)
└── +2 more field(s)
```

**Change:** In `ui.py` `packet_tree()`, replace list branch expansion with a single leaf: `f"[dim]{key} ({len(val)} item{'s' if len(val) != 1 else ''})[/dim]"`.

---

## Feature 3b — Inspect REPL at Checkpoints

**Problem:** At checkpoints, the user sees a summary but can't drill into specific findings or plan steps without opening log files manually.

**Solution:** Augment `_run_interrupt_loop` with a mini REPL before the `proceed/pause/redirect` decision.

**Commands available at any interrupt:**
| Input | Action |
|-------|--------|
| `findings` | List all researcher findings (numbered) |
| `plan` | List all thinker plan steps (numbered) |
| `f1`, `f3`, `f2` | Show full detail of finding N |
| `p2`, `p4` | Show full detail of plan step N |
| `assumptions` | Show all open assumptions from thinker |
| `notes` | Show critic minor notes |
| `proceed` | Continue (default) |
| `pause` | Pause the run |
| `redirect` | Redirect with new input |

**Implementation:** New `ui.inspect_repl(state_vals)` function that:
1. Prints a one-line hint: `[dim]Inspect: findings · plan · f1 · p2 · assumptions · notes · proceed[/dim]`
2. Loops `console.input()` until a routing command is received
3. On inspect commands, renders a Panel with the requested item details
4. Returns the routing response

**Item rendering example:**
```
╭──────── Finding 3 of 6 ────────╮
│ claim_type:  gap                │
│ tag_reason:  No KMS key policy  │
│ severity:    high               │
│ source:      IAM config review  │
╰─────────────────────────────────╯
```

**Finding detail fields** (from researcher packet `findings` array):
- `claim_type`, `claim`, `tag_reason`, `source`, `confidence`

**Plan step detail fields** (from thinker packet `plan` array):
- `step`, `assigned_to`, `description`, `researcher_commission`, `acceptance_criteria`

---

## Feature 3c — Live Status Bar

**Problem:** During long agent runs (Researcher 6× commissions = ~3 min), the terminal is either frozen (no spinner) or showing a spinner with no context about queue depth or progress.

**Solution:** A live panel pinned at the bottom showing the current agent, elapsed time, and queue depth.

**Live panel layout:**
```
╭──── ⟳ Running ──────────────────────────────────────────╮
│  🔬 Researcher  ·  commission 3/6  ·  18s  ·  3 queued  │
╰─────────────────────────────────────────────────────────╯
```

**Implementation:**
- `ui._run_status` dict (described above) holds current state
- `agent_header()` writes to `_run_status["current_agent"]` and `_run_status["current_start"]`
- `agent_result()` appends to `_run_status["completed"]` and increments `_run_status["phase_count"]`
- New `ui.make_status_panel()` renders the live panel from `_run_status`
- `_run_interrupt_loop` refactored into two-thread model (see Architecture above)

**Fallback:** If `LIVE_DISPLAY = False` env/flag, behaves exactly like today (backward compatible). Default: `True`.

---

## Feature 3d — Agent Run Stats Table

**Problem:** After a run completes, there's no summary of which agents ran, how many times, and how long each took.

**Solution:** Rich Table rendered by `run_complete()` using `_run_status["completed"]`.

**Output:**
```
─────────────────── Agent Run Stats ───────────────────
  Agent           Calls    Total Time
  ─────────────────────────────────────────────────
  Communicator    1        1.2s
  Thinker         2        28.4s
  Researcher      4        1m 12s
  Critic          1        8.3s
  ─────────────────────────────────────────────────
  Total           8        1m 50s
```

**Implementation:** `run_complete()` reads `_run_status["completed"]`, groups by agent name, sums elapsed, renders `rich.table.Table`.

---

## Implementation Order

```
3a  (collapsed lists)       — ui.py only, 5 min, no risk
3d  (run stats table)       — ui.py only, 15 min, no risk
3b  (inspect REPL)          — graph.py _run_interrupt_loop, 30 min, medium risk
3c  (live status bar)       — threading refactor, 45 min, higher risk
```

Each is independently shippable. Do 3a + 3d first (pure ui.py, zero regression risk), then 3b, then 3c.

---

## Files Changed

| File | Changes |
|------|---------|
| `ui.py` | `packet_tree` collapse fix · `inspect_repl()` · `make_status_panel()` · `_run_status` dict · `run_complete()` stats table · `run_start()` resets `_run_status` |
| `graph.py` | `_run_interrupt_loop` → two-thread model · inject `state_vals` into inspect REPL |

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Threading breaks LangGraph checkpoint state | Graph thread is isolated; only main thread calls `app.get_state()` / `app.update_state()` |
| `console.print()` inside `Live` races with display refresh | Rich handles this internally — prints go to scrollback safely |
| Live panel obscures output on narrow terminals | Panel height = 3 lines max; falls back to plain if terminal < 80 cols |
| Inspect REPL delays checkpoint response | Purely optional; `proceed` still works with no inspection |
| `_run_status` dict written from background thread, read from main | GIL-safe for simple dict ops; no explicit locks needed |
