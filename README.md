# My Autonomous Agent Framework

A multi-agent AI orchestration system for autonomous project execution — research, planning, development, QA, and self-improvement — with human-in-the-loop checkpoints and a hierarchical event-driven architecture.

> **Status:** End-to-end working · Research flow verified · Synthesis pipeline complete
> **Stack:** Python · LangGraph · `claude -p` subprocess (subscription auth) · Claude Sonnet 4.5 / Haiku 4.5

---

## What This Is

A locally-run, autonomous agent team that takes a task from human input through research, planning, implementation, and verification — producing real outputs (code, reports, documentation) while maintaining human oversight at key decision points.

The system is designed to run 24/7 on a personal computer, accumulate a logged history of its own work, improve its own prompts and architecture weekly, and scale from a single research task to a multi-week development project.

---

## Architecture

### Orchestration Pattern

Hierarchical event-driven architecture. Agents don't talk to each other directly — they produce structured JSON packets that the LangGraph orchestrator routes to the next agent in the chain. Human checkpoints interrupt the graph at defined stages.

```
Human Input
    │
    ▼
┌─────────────────────┐
│   Communicator      │  ← Parses intent, classifies task type
│   (Inbound)         │    loads TELOS only when needed
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐        ┌──────────────┐
│      Thinker        │───────▶│  Researcher  │  ← commissioned on demand
│                     │◀───────│              │    (max 3 iterations — safety cap)
│  Reality anchor     │        └──────────────┘
│  first-principles reframe
│  failure inversion
│  base rate anchor
│  uncertainty decomposition
│  multi-lens triangulation
│  deductive derivation chain
│
│  [synthesized] ────────────────────────────────────────────────────────────┐
│   When returning from Researcher: synthesizes findings,                    │
│   writes artifact to logs/, returns status="synthesized"                   │
└────────┬────────────┘                                                      │
         │  (skipped for pure research tasks)                                │
         ▼                                                                   │
┌─────────────────────┐                                                      │
│      Critic         │  ← 7 adversarial checks including intuition-collapse │
│                     │    rejects → Thinker retry (max 3)                   │
└────────┬────────────┘                                                      │
         │                                                                   │
    [CHECKPOINT] ← Human approves plan before any development begins        │
         │                                                                   │
         ▼
┌─────────────────────┐        ┌──────────────┐
│   Lead Engineer     │───────▶│  Researcher  │  ← tool validation
│                     │        └──────────────┘
│  Architecture       │
│  Task decomposition │
│  DoD per task       │
│  Git governance     │
│  Integration batch  │
└────────┬────────────┘
         │  (one task at a time)
         ▼
┌─────────────────────┐
│     Developer       │  ← implements, writes unit tests, full docstrings
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│        QA           │  ← verifies DoD, security checks, regression
│                     │    fail → Lead Engineer (max 2 retries)
└────────┬────────────┘
         │
    [CHECKPOINT] ← Human approves merge
         │
         ▼
┌─────────────────────┐
│  Integration Agent  │  ← contract verification across module boundaries
│                     │    triggered after batch merge
└────────┬────────────┘
         │
         ▼                                                                   │
┌─────────────────────┐◀───────────────────────────────────────────────────┘
│   Communicator      │  ← formats final output for human
│   (Outbound)        │    research synthesis: 6-bullet plain-text summary + artifact path
└─────────────────────┘

         ↕  weekly / on-demand
┌─────────────────────┐
│ System Improvement  │  ← reads all agent logs, identifies bottlenecks,
│      Agent          │    produces prioritized improvement backlog
└─────────────────────┘
```

---

## Agent Roster

| Agent | Role | Model | Key Output |
|---|---|---|---|
| **Communicator** | Human ↔ system interface, task classification | Haiku | Inbound task packet, outbound summaries |
| **Thinker** | Reality-anchored strategic planning | Sonnet | Plan with derivation chain, pre-mortem, assumption log |
| **Critic** | Adversarial plan review (7 checks) | Sonnet | Verdict: approved / revise / reject |
| **Researcher** | Source-filtered live web search via native Claude WebSearch tool | Haiku | Knowledge report with claim-type tagging |
| **Lead Engineer** | Architecture, task decomposition, git governance | Sonnet | Atomic tasks with DoD, integration batch definition |
| **Developer** | Implementation, unit tests, documentation | Sonnet | Code, tests, docstrings |
| **QA** | Task-level verification, security checks, regression | Haiku | Pass/fail with severity-tiered findings |
| **Integration Agent** | Cross-module contract verification after batch merge | Haiku | Contract map, critical path test, regression findings |
| **System Improvement** | Weekly meta-audit of all agent logs | Sonnet | Severity-tagged bottleneck backlog, decision journal |

### Agent Activation Logic

- **Critic** is skipped for pure research tasks (no `lead_engineer` or `developer` steps in plan)
- **Thinker** enters synthesis mode when `PRIOR_OUTPUT` contains researcher findings — skips all planning, writes a full artifact `.md`, returns `status: "synthesized"` → routes directly to Communicator
- **Researcher** has a hard iteration cap (`MAX_RESEARCHER_ITERATIONS = 3`) to prevent infinite research loops
- **Telos** (user goals file) is injected into Thinker only when `task_type` is `strategy`, `goal_setting`, or `project_definition` — not during project execution
- **Integration Agent** fires after Lead Engineer defines a `trigger_after_tasks` batch, not after every task
- **System Improvement Agent** runs on a 7-day schedule or on-demand; its Thinker/Researcher sub-calls bypass human checkpoints by design

---

## Human Checkpoints

The graph pauses for human approval at five points:

| Checkpoint | Trigger | Decision |
|---|---|---|
| `thinker_plan_review` | Critic approves plan | Proceed to development / pause / redirect |
| `qa_pass_merge_approval` | QA passes a task | Approve merge / pause |
| `integration_failure_review` | Integration Agent finds P0/P1 failures | Review before Lead Engineer re-assigns |
| `sia_critical_findings` | System Improvement flags critical issues | Approve before infrastructure changes |
| `destructive_operation` | Any merge to main, force push, schema migration | Always requires explicit approval |

---

## Communication Protocol

All inter-agent communication uses **compressed JSON packets**. Full outputs go to log files. Agents pass only the packet forward.

```json
{
  "status": "complete",
  "summary": "2-3 sentences",
  "key_outputs": ["..."],
  "escalate_to": null,
  "log_ref": "logs/agent_taskslug.md"
}
```

This keeps per-call token cost low regardless of how verbose the agent's internal reasoning was.

---

## Key Design Decisions

**Why LangGraph over a simpler orchestrator?**  
Human-in-the-loop checkpoints (`interrupt_before`) and stateful graph traversal are first-class features. The graph state (`AgentSystemState`) is the single source of truth for all routing decisions. Alternatives (raw Python loops, CrewAI) require custom-building what LangGraph provides natively.

**Why not peer-to-peer agent communication?**  
Hard to debug, hard to checkpoint, expensive to trace. All routing goes through the orchestrator. Agents produce packets; the graph decides what happens next.

**Why separate QA and Integration Agent?**  
QA is task-scoped (does this module work in isolation?). Integration Agent is system-scoped (do modules work together?). These are different failure modes that require different test strategies and different timing.

**Why Critic is skipped for research tasks?**  
The Critic's intuition-collapse check flags "generic steps" as framework failure. For a research task, generic steps (search → integrate → synthesize) are correct. Applying Critic to research produces false rejects. Critic fires only when the plan contains `lead_engineer` or `developer` assignments.

**Why model tiering?**  
Sonnet for reasoning-heavy agents (Thinker, Critic, Lead Engineer, Developer), Haiku or Gemini Flash for structured-execution agents (QA, Researcher, Communicator, Integration Agent). 5× cost difference between tiers with minimal quality loss on structured tasks.

**Why prompt caching with 5-minute TTL over 1-hour TTL?**  
For 24/7 continuous operation, agents are called multiple times per 5-minute window — the cache refreshes naturally on each hit. 1-hour TTL costs 2× per write vs 1.25× for 5-minute, producing higher write costs without additional read savings at this call frequency.

---

## File Structure

```
project/
├── graph.py                    # LangGraph orchestrator, all nodes and routing
├── state.py                    # AgentSystemState TypedDict — single source of truth
├── sources.yaml                # Researcher source tier list (white/blacklist)
├── CLAUDE.md                   # Local override: disables PAI format, enables tool use for subagents
├── .env                        # TAVILY_API_KEY (reserved for future Tavily integration)
│
│   ── Agent system prompts (flat, at root) ──
├── communicator.md
├── thinker_v2-2.md
├── critic.md
├── researcher.md
├── lead_engineer.md
├── developer.md
├── qa.md
├── integration_agent.md
└── system_improvement_agent.md
│
├── project/
│   ├── progress.md             # Live task board — max 250 lines, Lead Engineer owns
│   ├── adr_log.md              # Architecture decision log
│   └── tech_stack.md           # Current stack decisions
│
├── improvement/
│   ├── backlog.json            # System Improvement Agent's prioritized backlog
│   ├── decision_journal.json   # Prediction tracking for calibration
│   └── calibration_notes.md
│
└── logs/                       # All agent outputs + synthesis artifacts
    ├── thinker_*.md
    ├── critic_*.md
    ├── researcher_*.md
    ├── synthesis_*.md          # Written by Thinker in synthesis mode
    ├── lead_engineer_*.md
    ├── developer_*.md
    ├── qa_*.md
    ├── integration_*.md
    └── system_improvement_*.md
```

---

## Setup

```bash
# 1. Install dependencies
pip install langgraph python-dotenv

# 2. Authenticate (uses subscription — no API key needed)
# Requires Claude CLI installed and logged in:
#   npm install -g @anthropic-ai/claude-code
#   claude login

# 3. Create required directories
mkdir -p logs project improvement
echo "# Progress" > project/progress.md
echo "# ADR Log" > project/adr_log.md
echo "# Tech Stack" > project/tech_stack.md
echo "{}" > improvement/decision_journal.json
echo "[]" > improvement/backlog.json

# 4. Run
python3 graph.py "your task here"
```

---

## Running Tasks

```bash
# Research task — Communicator → Thinker → Researcher → Thinker (synthesis) → Communicator
# Outputs 6-bullet summary to terminal + full artifact in logs/synthesis_*.md
python3 graph.py "research the current state of AI agent frameworks in 2026"

# Development task — full pipeline including Critic, Lead Engineer, Developer, QA
python3 graph.py "build a REST API endpoint for user authentication using FastAPI"

# Strategy task — includes Telos injection
python3 graph.py "review my current project priorities and suggest what to work on next"

# Run System Improvement Agent manually
python3 graph.py "--sia"
```

The graph pauses at checkpoints and prints:
```
──────────────────────────────────────────────────
🛑 CHECKPOINT: critic_verdict_review
Summary: Plan approved with 2 minor notes — see logs/critic_task.md
Options: proceed / pause / redirect
──────────────────────────────────────────────────
```

Resume after checkpoint:
```python
from langgraph.types import Command
app.invoke(Command(resume={"human_decision": "proceed"}), config)
```

---

## Cost Reference

All figures assume Sonnet 4.6 ($3/$15 per MTok input/output) and Haiku 4.5 ($1/$5):

| Usage level | Tasks/month | Est. monthly cost |
|---|---|---|
| Light development | 50–100 tasks | $15–$40 |
| Active daily use | 300–500 tasks | $80–$160 |
| 24/7 autonomous | 2,000+ tasks | $200–$400 |

**Optimization levers:**
- Subscription auth via `claude -p` — no per-token API billing while on Claude Max subscription
- If switching to API billing: route QA/Researcher/Communicator to Haiku ($0.80/$4 per MTok) vs Sonnet ($3/$15): ~4× cheaper for execution agents
- Tavily API reserved for future fast web search (currently using native Claude WebSearch tool)

---

## Roadmap

- [x] Agent system prompts — all 9 agents
- [x] LangGraph graph + state schema
- [x] Human checkpoint mechanism
- [x] Thinker→Critic retry loop with max iterations (max 3)
- [x] QA failure routing with retry counter (max 2 per task)
- [x] Integration Agent batch trigger logic
- [x] Print-based terminal visibility with per-agent status lines
- [x] Researcher live web search via native Claude WebSearch tool
- [x] Thinker synthesis mode — findings → artifact .md → 6-bullet terminal summary
- [x] Researcher iteration safety cap (max 3 cycles)
- [x] Communicator outbound: plain-text printer (no JSON parse crash)
- [x] Telos file integration and goal-aware task routing
- [x] `CLAUDE.md` local override — disables PAI format for subagents, enables tool use
- [ ] SQLite checkpointing for crash recovery
- [ ] `rich` terminal display with live agent status + timing
- [ ] Developer real code execution + error feedback loop (Bash tool in Developer agent)
- [ ] Multi-researcher support — parallel commissions for multi-step research plans
- [ ] Tavily integration for faster web search (at scale)
- [ ] Website / log hosting for project summaries

---

## Notes for AI Assistants Reading This

Agent `.md` files live at the project root (not in a subdirectory) and are loaded as system prompts at runtime. Each defines: role, runtime injection format, thinking discipline, output JSON schema, and scope boundaries.

`CLAUDE.md` at the project root is a local override loaded by every `claude -p` subprocess. It disables the PAI Algorithm format (so agents output pure JSON, not markdown), and explicitly enables tool use (WebSearch/WebFetch for Researcher, Bash/Edit for Developer).

`state.py` is the canonical contract for all inter-agent communication. `graph.py` contains all routing logic — agent files contain no routing instructions.

When modifying agent behavior: edit the relevant `.md` file. When modifying routing: edit `graph.py`. When adding state fields: update `state.py` and the relevant `_build_injection()` call in `graph.py`.

Human-facing output (communicator outbound, human checkpoints) uses `_run_agent_raw()` which prints stdout directly without JSON parsing. All other agents use `_run_agent()` which returns a parsed dict for state updates.

---

*Built iteratively — architecture designed before implementation, each agent verified before wiring, integration audited after each layer.*
