"""
Agent System State — TypedDict for LangGraph StateGraph

All fields are optional at initialization. The orchestrator populates them
as agents complete their work. The state is the single source of truth
for routing decisions and inter-agent handoffs.
"""

from typing import TypedDict, Optional, Literal, Any


# ─────────────────────────────────────────────
# Sub-types (inline dicts for JSON packets)
# ─────────────────────────────────────────────

AgentStatus = Literal["complete", "blocked", "needs_human_input", "needs_research", "pass", "fail", "pass_with_notes", "synthesized"]
TaskType = Literal["project_work", "goal_setting", "project_definition", "strategy"]
CheckpointType = Literal[
    "thinker_plan_review",
    "thinker_needs_clarification",
    "critic_verdict_review",
    "qa_pass_merge_approval",
    "integration_failure_review",
    "sia_critical_findings",
    "destructive_operation",
]


class AgentSystemState(TypedDict, total=False):
    # ── Session ──────────────────────────────────────────────────────────────
    today: str                          # ISO date injected at session start
    project_context: str                # One-paragraph current project state
    telos_source_path: str              # Path to TELOS goals file (loaded when telos_required=True)
    logs_directory: str                 # Root path for all agent logs
    progress_file: str                  # Path to project/progress.md
    adl_file: str                       # Path to project/adr_log.md
    tech_stack_file: str                # Path to project/tech_stack.md
    sources_file: str                   # Path to sources.yaml (Researcher)
    decision_journal: str               # Path to improvement/decision_journal.json
    prior_backlog: str                  # Path to improvement/backlog.json

    # ── Communicator Inbound ──────────────────────────────────────────────────
    human_input: str                    # Raw human message
    communicator_task: str              # Precise restatement of what human wants
    task_type: TaskType                 # project_work | goal_setting | project_definition | strategy
    telos_required: bool                # Whether orchestrator should load TELOS before Thinker
    clarification_asked: bool           # Whether Communicator asked human a question
    clarification_question: Optional[str]  # The single clarification question asked
    ready_to_proceed: bool              # False = halt, wait for human response

    # ── Thinker ───────────────────────────────────────────────────────────────
    thinker_status: AgentStatus
    thinker_packet: dict[str, Any]      # Full Thinker JSON output
    thinker_real_question: str          # Reframed question
    thinker_plan: list[dict[str, Any]]  # Plan steps with assigned_to and researcher_commission
    thinker_open_assumptions: list[dict[str, Any]]
    thinker_log_ref: str
    thinker_retry_count: int            # Tracks Thinker→Critic retry cycles (max 3)

    # ── Critic ────────────────────────────────────────────────────────────────
    critic_status: Literal["approved", "revise", "reject"]
    critic_packet: dict[str, Any]       # Full Critic JSON output
    critic_verdict: str                 # "approved" | "revise" | "reject"
    critic_minor_notes: list[str]       # Non-blocking notes passed to Lead Engineer
    critic_open_assumptions: list[dict[str, Any]]  # Assumptions Critic flagged unresolved
    critic_log_ref: str

    # ── Researcher ────────────────────────────────────────────────────────────
    research_commissioner: str          # "thinker" | "lead_engineer" | "system_improvement"
    research_commission: dict[str, Any] # Canonical commission packet
    researcher_status: AgentStatus
    researcher_packet: dict[str, Any]   # Full Researcher JSON output
    researcher_findings: list[dict[str, Any]]
    researcher_log_ref: str
    pending_research: list[dict[str, Any]]  # Queue of non-blocking research commissions
    researcher_iteration_count: int         # Safety cap tracker (max MAX_RESEARCHER_ITERATIONS)

    # ── Lead Engineer ─────────────────────────────────────────────────────────
    lead_engineer_status: AgentStatus
    lead_engineer_packet: dict[str, Any]
    task_queue: list[dict[str, Any]]    # All tasks decomposed by Lead Engineer
    current_task: dict[str, Any]        # Task currently assigned to Developer
    integration_batch: dict[str, Any]   # {batch_id, trigger_after_tasks, fire_integration_on_merge}
    prior_qa_failures: list[dict[str, Any]]  # Recent QA failures passed back to Lead Engineer
    lead_engineer_log_ref: str

    # ── Developer ─────────────────────────────────────────────────────────────
    developer_status: Literal["complete", "needs_clarification", "blocked"]
    developer_packet: dict[str, Any]
    developer_log_ref: str
    # If developer needs clarification, orchestrator routes back to Lead Engineer
    clarification_request: Optional[dict[str, Any]]  # {task_id, ambiguity, question}

    # ── QA ────────────────────────────────────────────────────────────────────
    qa_status: Literal["pass", "fail", "pass_with_notes"]
    qa_packet: dict[str, Any]
    qa_findings: list[dict[str, Any]]
    qa_integration_tests_written: list[str]  # Paths to integration test files QA wrote
    qa_blocking_threshold: str          # "p0_p1" — from Lead Engineer task packet
    qa_retry_count: dict[str, int]      # {task_id: retry_count} — tracks per-task retries (max 2)
    qa_log_ref: str

    # ── Integration Agent ─────────────────────────────────────────────────────
    integration_status: Literal["pass", "fail", "pass_with_notes"]
    integration_packet: dict[str, Any]
    integration_batch_id: str
    integration_trigger: Literal["batch_merge", "milestone", "on_demand"]
    prior_integration_log: str          # Path to most recent prior integration run log
    integration_log_ref: str

    # ── System Improvement Agent ──────────────────────────────────────────────
    sia_status: AgentStatus
    sia_packet: dict[str, Any]
    sia_run_type: Literal["scheduled", "on_demand"]
    sia_system_health: Literal["healthy", "degrading", "critical"]
    sia_immediate_tasks: list[dict[str, Any]]
    sia_log_ref: str

    # ── Human Checkpoint ─────────────────────────────────────────────────────
    checkpoint_required: bool           # True when system must pause for human approval
    checkpoint_type: CheckpointType     # Determines which node handles post-approval routing
    checkpoint_stage: str               # Label shown in Communicator checkpoint message
    human_decision: Literal["proceed", "pause", "redirect"]  # Human's response

    # ── Routing / Control ─────────────────────────────────────────────────────
    next_agent: str                     # Explicit override for orchestrator routing (rarely used)
    error: Optional[str]                # Set when an agent returns an unrecoverable error
    completed: bool                     # True when the full workflow has finished
