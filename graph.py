"""
Agent System Orchestrator — LangGraph StateGraph

Wires all custom agents into a directed workflow. Each node:
  1. Builds the runtime injection for that agent
  2. Calls the agent via `claude -p` (subscription auth, no API key)
  3. Parses the JSON packet from agent output
  4. Writes result fields back to state

Routing:
  - Conditional edges read state fields set by nodes
  - Human checkpoints use interrupt() inside human_checkpoint node with MemorySaver
  - Thinker→Critic retry loop bounded by thinker_retry_count (max 3)
  - QA retry bounded by qa_retry_count[task_id] (max 2)

Usage:
    from graph import app
    result = app.invoke(initial_state)

Or with human-in-the-loop:
    # Runs until interrupt
    partial = app.invoke(initial_state)
    # Human reviews, then resume:
    final = app.invoke(Command(resume="proceed"), config)
"""

import json
import subprocess
from pathlib import Path
from typing import Optional

from langgraph.graph import StateGraph, END
from langgraph.types import Command, interrupt
from langgraph.checkpoint.memory import MemorySaver

from state import AgentSystemState


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

AGENTS_DIR = Path(__file__).parent
TELOS_PATH = Path.home() / ".claude/skills/PAI/USER/TELOS/goals.md"
MAX_THINKER_RETRIES = 3
MAX_QA_RETRIES = 2
MAX_RESEARCHER_ITERATIONS = 3
VERBOSE = True  # Set False to suppress full packet output

AGENT_MODELS = {
    "thinker":                   "claude-sonnet-4-5",
    "critic":                    "claude-sonnet-4-5",
    "lead_engineer":             "claude-sonnet-4-5",
    "developer":                 "claude-sonnet-4-5",
    "system_improvement_agent":  "claude-sonnet-4-5",
    "researcher":                "claude-haiku-4-5-20251001",
    "qa":                        "claude-haiku-4-5-20251001",
    "communicator":              "claude-haiku-4-5-20251001",
    "integration_agent":         "claude-haiku-4-5-20251001",
}


def _load_agent_prompt(filename: str) -> str:
    """Read agent .md file and return its contents as the system prompt."""
    return (AGENTS_DIR / filename).read_text()


def _run_agent_raw(agent_prompt: str, injection: str, model: Optional[str] = None) -> None:
    """
    Run an agent via `claude -p` and print its output directly to stdout.
    Used for human-facing output (communicator_outbound, human_checkpoint)
    where the response is plain text, not a JSON packet.
    """
    full_prompt = f"{injection}\n\n---\n\n{agent_prompt}"
    cmd = ["claude", "--dangerously-skip-permissions", "-p"]
    if model:
        cmd += ["--model", model]
    result = subprocess.run(
        cmd,
        input=full_prompt,
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(AGENTS_DIR),
    )
    if result.returncode != 0:
        raise RuntimeError(f"Agent call failed:\nSTDERR: {result.stderr}\nSTDOUT: {result.stdout}")
    print(result.stdout.strip(), flush=True)


def _run_agent(agent_prompt: str, injection: str, model: Optional[str] = None) -> dict:
    """
    Run an agent via `claude -p` using subscription auth (no API key).
    The injection is prepended to the prompt as a runtime context block.
    Returns the parsed JSON packet from agent output.
    """
    full_prompt = f"{injection}\n\n---\n\n{agent_prompt}"
    cmd = ["claude", "--dangerously-skip-permissions", "-p"]
    if model:
        cmd += ["--model", model]
    result = subprocess.run(
        cmd,
        input=full_prompt,
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(AGENTS_DIR),  # loads local CLAUDE.md, not global PAI
    )

    if result.returncode != 0:
        print(f"\n[ERROR] returncode: {result.returncode}", flush=True)
        if result.stderr:
            print(f"[ERROR] stderr: {result.stderr[:400]}", flush=True)
        raise RuntimeError(f"Agent call failed:\nSTDERR: {result.stderr}\nSTDOUT: {result.stdout}")

    output = result.stdout.strip()
    if not output:
        raise RuntimeError(
            f"Agent returned empty output.\n"
            f"Prompt length: {len(full_prompt)} chars\n"
            f"stderr: {result.stderr[:200]}"
        )

    if "```json" in output:
        output = output.split("```json")[1].split("```")[0].strip()

    try:
        packet = json.loads(output)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Agent output was not valid JSON.\n"
            f"Parse error: {e}\n"
            f"Raw output (first 500 chars):\n{output[:500]}"
        ) from e

    if VERBOSE:
        print(f"\n{json.dumps(packet, indent=2)}\n", flush=True)

    return packet


def _build_injection(**kwargs) -> str:
    """Build the runtime injection block passed to each agent."""
    lines = []
    for key, value in kwargs.items():
        if value is not None:
            lines.append(f"{key.upper()}: {value}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# Nodes
# ─────────────────────────────────────────────

def communicator_inbound(state: AgentSystemState) -> AgentSystemState:
    """Parse and classify human input. Produce structured task packet."""
    print(f"\n📨 [Communicator/{AGENT_MODELS['communicator']}] Parsing input...", flush=True)
    prompt = _load_agent_prompt("communicator.md")
    injection = _build_injection(
        today=state.get("today"),
        mode="inbound",
        human_input=state.get("human_input"),
        project_context=state.get("project_context", ""),
    )
    packet = _run_agent(prompt, injection, AGENT_MODELS["communicator"])
    print(f"   ✓ {packet.get('task_type', '?')} — {packet.get('task', '')}", flush=True)

    updates: AgentSystemState = {
        "communicator_task": packet.get("task", ""),
        "task_type": packet.get("task_type", "project_work"),
        "telos_required": packet.get("telos_required", False),
        "clarification_asked": packet.get("clarification_asked", False),
        "clarification_question": packet.get("clarification_question"),
        "ready_to_proceed": packet.get("ready_to_proceed", True),
    }
    # If telos_required, capture the source path from packet or use default
    if packet.get("telos_required"):
        updates["telos_source_path"] = packet.get("telos_source_path", str(TELOS_PATH))

    return updates


def thinker(state: AgentSystemState) -> AgentSystemState:
    """Produce a reasoned plan. Applies reality anchor, inversion, base rate."""
    retry = state.get("thinker_retry_count", 0)
    retry_tag = f" (retry {retry})" if retry > 0 else ""
    print(f"\n🧠 [Thinker/{AGENT_MODELS['thinker']}] Planning{retry_tag}...", flush=True)
    prompt = _load_agent_prompt("thinker_v2-2.md")

    # Load TELOS only when required by Communicator
    telos_content = ""
    if state.get("telos_required"):
        telos_path = state.get("telos_source_path", str(TELOS_PATH))
        telos_content = Path(telos_path).read_text()

    # PRIOR_OUTPUT: inject human clarification, researcher findings, or Critic summary
    # in priority order.
    prior_output = ""
    if state.get("checkpoint_type") == "thinker_needs_clarification" and state.get("human_decision"):
        # Retry after human answered clarification questions
        prior_output = (
            f"HUMAN CLARIFICATION RESPONSE: {state.get('human_decision')}\n"
            f"The human has answered your questions. Now produce a concrete, actionable plan."
        )
    elif state.get("researcher_findings") and state.get("research_commissioner") == "thinker":
        findings = state.get("researcher_findings", [])
        r_summary = state.get("researcher_packet", {}).get("summary", "")
        prior_output = (
            f"RESEARCHER FINDINGS:\n{json.dumps(findings, indent=2)}\n\n"
            f"RESEARCHER SUMMARY: {r_summary}"
        )
    elif state.get("critic_packet"):
        prior_output = state["critic_packet"].get("summary", "")

    injection = _build_injection(
        today=state.get("today"),
        task=state.get("communicator_task"),
        task_type=state.get("task_type"),
        project_context=state.get("project_context", ""),
        telos=telos_content or None,
        prior_output=prior_output or None,
    )
    packet = _run_agent(prompt, injection, AGENT_MODELS["thinker"])
    print(f"   ✓ {packet.get('status', '?')} — {packet.get('summary', '')}", flush=True)

    plan = packet.get("plan", [])

    # If routing will go directly to researcher, pre-load the first commission
    first_research_step = next(
        (s for s in plan if s.get("assigned_to") == "researcher" and s.get("researcher_commission")),
        None,
    )

    updates: AgentSystemState = {
        "thinker_status": packet.get("status", "complete"),
        "thinker_packet": packet,
        "thinker_real_question": packet.get("real_question", ""),
        "thinker_plan": plan,
        "thinker_open_assumptions": packet.get("open_assumptions", []),
        "thinker_log_ref": packet.get("log_ref", ""),
        "thinker_retry_count": state.get("thinker_retry_count", 0),  # incremented by critic node
        "research_commissioner": "thinker",
        # Clear any previous checkpoint/clarification state on each Thinker run
        "checkpoint_type": None,
        "human_decision": None,
    }
    if packet.get("status") == "needs_human_input":
        updates["checkpoint_type"] = "thinker_needs_clarification"
        updates["checkpoint_stage"] = "thinker_needs_clarification"
        updates["checkpoint_required"] = True
    if first_research_step:
        updates["research_commission"] = first_research_step["researcher_commission"]

    return updates


def critic(state: AgentSystemState) -> AgentSystemState:
    """Adversarially review the Thinker's plan. Can reject or approve."""
    print(f"\n⚖️  [Critic/{AGENT_MODELS['critic']}] Reviewing plan...", flush=True)
    prompt = _load_agent_prompt("critic.md")
    injection = _build_injection(
        today=state.get("today"),
        task=state.get("communicator_task"),
        task_type=state.get("task_type"),
        project_context=state.get("project_context", ""),
        thinker_packet=json.dumps(state.get("thinker_packet", {})),
        thinker_log_ref=state.get("thinker_log_ref"),
    )
    packet = _run_agent(prompt, injection, AGENT_MODELS["critic"])
    print(f"   ✓ {packet.get('verdict', '?')} — {packet.get('summary', '')}", flush=True)

    verdict = packet.get("verdict", "approved")
    # Increment here so route_after_critic reads the updated count (not pre-thinker count)
    new_retry_count = state.get("thinker_retry_count", 0) + (1 if verdict in ("revise", "reject") else 0)

    return {
        "critic_status": verdict,
        "critic_packet": packet,
        "critic_verdict": verdict,
        "critic_minor_notes": packet.get("minor_notes", []),
        "critic_open_assumptions": packet.get("open_assumptions", []),
        "critic_log_ref": packet.get("log_ref", ""),
        "thinker_retry_count": new_retry_count,
    }


def researcher(state: AgentSystemState) -> AgentSystemState:
    """Execute a research commission. Routes back to the commissioning agent."""
    commission = state.get("research_commission", {})
    q = commission.get("question", "")
    print(f"\n🔬 [Researcher/{AGENT_MODELS['researcher']}] Searching: {q}...", flush=True)
    prompt = _load_agent_prompt("researcher.md")
    commission = state.get("research_commission", {})

    injection = _build_injection(
        today=state.get("today"),
        task=commission.get("question"),
        claim_type=commission.get("claim_type"),
        depth_required=commission.get("depth_required"),
        sources_file=state.get("sources_file"),
        project_context=commission.get("context"),
        prior_output=None,
    )
    packet = _run_agent(prompt, injection, AGENT_MODELS["researcher"])
    n = len(packet.get("findings", []))
    print(f"   ✓ {packet.get('status', '?')} — {n} finding(s). {packet.get('summary', '')}", flush=True)

    return {
        "researcher_status": packet.get("status", "complete"),
        "researcher_packet": packet,
        "researcher_findings": packet.get("findings", packet.get("key_findings", [])),
        "researcher_log_ref": packet.get("log_ref", ""),
        "researcher_iteration_count": state.get("researcher_iteration_count", 0) + 1,
    }


def lead_engineer(state: AgentSystemState) -> AgentSystemState:
    """Decompose plan into tasks with DoD. Governs git. Handles QA failures."""
    print("\n🔧 [Lead Engineer] Decomposing tasks...", flush=True)
    prompt = _load_agent_prompt("lead_engineer.md")

    # Critic verdict summary for injection
    critic_verdict_summary = {}
    if state.get("critic_packet"):
        cp = state["critic_packet"]
        critic_verdict_summary = {
            "verdict": cp.get("verdict"),
            "minor_notes": cp.get("minor_notes", []),
            "open_assumptions": cp.get("open_assumptions", []),
        }

    injection = _build_injection(
        today=state.get("today"),
        task=json.dumps(state.get("thinker_packet", {})),
        critic_verdict=json.dumps(critic_verdict_summary),
        project_context=state.get("project_context", ""),
        tech_stack=state.get("tech_stack_file"),
        progress_file=state.get("progress_file"),
        adl_file=state.get("adl_file"),
        logs_directory=state.get("logs_directory"),
        prior_qa_failures=json.dumps(state.get("prior_qa_failures", [])),
    )
    packet = _run_agent(prompt, injection, AGENT_MODELS["lead_engineer"])
    n = len(packet.get("tasks", []))
    print(f"   ✓ {packet.get('status', '?')} — {n} task(s) defined. {packet.get('summary', '')}", flush=True)

    tasks = packet.get("tasks", [])
    return {
        "lead_engineer_status": packet.get("status", "complete"),
        "lead_engineer_packet": packet,
        "task_queue": tasks,
        "integration_batch": packet.get("integration_batch"),
        "lead_engineer_log_ref": packet.get("log_ref", ""),
        "current_task": tasks[0] if tasks else None,
    }


def developer(state: AgentSystemState) -> AgentSystemState:
    """Implement the current task. May return clarification_request."""
    task = state.get("current_task", {})
    task_id = task.get("task_id", "?")
    title = task.get("title", "")[:50]
    print(f"\n💻 [Developer] Implementing {task_id}: {title}...", flush=True)
    prompt = _load_agent_prompt("developer.md")

    # Check if this is a clarification response from Lead Engineer
    prior_output = ""
    if state.get("clarification_request"):
        # This run is after LE responded — pass the clarification as prior output
        prior_output = json.dumps(state.get("lead_engineer_packet", {}).get("clarification_response", {}))

    injection = _build_injection(
        today=state.get("today"),
        task=json.dumps(task),
        project_context=state.get("project_context", ""),
        progress_file=state.get("progress_file"),
        logs_directory=state.get("logs_directory"),
        prior_output=prior_output or None,
    )
    packet = _run_agent(prompt, injection, AGENT_MODELS["developer"])
    status = packet.get("status", "?")
    icon = "✓" if status == "complete" else "⚠"
    print(f"   {icon} {status} — {packet.get('summary', '')}", flush=True)

    updates: AgentSystemState = {
        "developer_status": packet.get("status", "complete"),
        "developer_packet": packet,
        "developer_log_ref": packet.get("log_ref", ""),
    }
    if packet.get("status") == "clarification_needed":
        updates["clarification_request"] = packet.get("clarification_request")
    else:
        updates["clarification_request"] = None

    return updates


def qa(state: AgentSystemState) -> AgentSystemState:
    """Run quality checks against the task DoD. May fail back to Lead Engineer."""
    task = state.get("current_task", {})
    task_id = task.get("task_id", "?")
    print(f"\n🔍 [QA] Verifying {task_id}...", flush=True)
    prompt = _load_agent_prompt("qa.md")

    injection = _build_injection(
        today=state.get("today"),
        task_packet=json.dumps(task),
        developer_packet=json.dumps(state.get("developer_packet", {})),
        developer_log_ref=state.get("developer_log_ref"),
        progress_file=state.get("progress_file"),
        logs_directory=state.get("logs_directory"),
        blocking_threshold=task.get("blocking_threshold", "p0_p1"),
    )
    packet = _run_agent(prompt, injection, AGENT_MODELS["qa"])
    status = packet.get("status", "?")
    n_fail = len([f for f in packet.get("findings", []) if f.get("severity") in ("p0", "p1")])
    icon = "✓" if status == "pass" else "✗"
    print(f"   {icon} {status} — {n_fail} blocking finding(s). {packet.get('summary', '')}", flush=True)

    # Track per-task retry count
    task_id = task.get("task_id", "unknown")
    qa_retry_count = dict(state.get("qa_retry_count", {}))
    qa_status = packet.get("status", "pass")
    if qa_status == "fail":
        qa_retry_count[task_id] = qa_retry_count.get(task_id, 0) + 1

    return {
        "qa_status": qa_status,
        "qa_packet": packet,
        "qa_findings": packet.get("findings", []),
        "qa_integration_tests_written": packet.get("integration_tests_written", []),
        "qa_retry_count": qa_retry_count,
        "qa_log_ref": packet.get("log_ref", ""),
    }


def integration_agent(state: AgentSystemState) -> AgentSystemState:
    """Test module boundaries after a batch merge."""
    batch = state.get("integration_batch", {})
    batch_id = batch.get("batch_id", "?")
    print(f"\n🔗 [Integration] Testing batch {batch_id}...", flush=True)
    prompt = _load_agent_prompt("integration_agent.md")

    # Gather task packets for all tasks in this batch
    merged_task_ids = batch.get("trigger_after_tasks", [])
    merged_task_packets = [
        t for t in state.get("task_queue", [])
        if t.get("task_id") in merged_task_ids
    ]

    injection = _build_injection(
        today=state.get("today"),
        trigger=state.get("integration_trigger", "batch_merge"),
        batch_id=batch.get("batch_id"),
        merged_tasks=json.dumps(merged_task_ids),
        task_packets=json.dumps(merged_task_packets),
        qa_integration_tests=json.dumps(state.get("qa_integration_tests_written", [])),
        prior_integration_log=state.get("prior_integration_log", "none"),
        logs_directory=state.get("logs_directory"),
        progress_file=state.get("progress_file"),
        project_context=state.get("project_context", ""),
    )
    packet = _run_agent(prompt, injection, AGENT_MODELS["integration_agent"])
    print(f"   ✓ {packet.get('status', '?')} — {packet.get('summary', '')}", flush=True)

    log_ref = packet.get("log_ref", "")
    return {
        "integration_status": packet.get("status", "pass"),
        "integration_packet": packet,
        "integration_log_ref": log_ref,
        "prior_integration_log": log_ref,
    }


def system_improvement_agent(state: AgentSystemState) -> AgentSystemState:
    """Weekly meta-audit. Reads all agent logs and produces improvement backlog."""
    print("\n🔄 [SIA] Running system audit...", flush=True)
    prompt = _load_agent_prompt("system_improvement_agent.md")
    injection = _build_injection(
        today=state.get("today"),
        run_type=state.get("sia_run_type", "scheduled"),
        period_start="7 days ago",
        period_end=state.get("today"),
        log_directory=state.get("logs_directory"),
        decision_journal=state.get("decision_journal"),
        prior_backlog=state.get("prior_backlog"),
        project_context=state.get("project_context", ""),
    )
    packet = _run_agent(prompt, injection, AGENT_MODELS["system_improvement_agent"])
    health = packet.get("system_health", "?")
    icon = "✓" if health == "healthy" else "⚠"
    print(f"   {icon} {health} — {packet.get('summary', '')}", flush=True)

    return {
        "sia_status": packet.get("status", "complete"),
        "sia_packet": packet,
        "sia_system_health": packet.get("system_health", "healthy"),
        "sia_immediate_tasks": packet.get("immediate_tasks", []),
        "sia_log_ref": packet.get("log_ref", ""),
    }


def human_checkpoint(state: AgentSystemState) -> AgentSystemState:
    """
    Pause point — LangGraph interrupts here before execution.

    The Communicator formats what the human sees.
    After the human replies, the graph resumes with human_decision set.
    """
    stage = state.get("checkpoint_stage", "unknown")
    print(f"\n⏸️  [Checkpoint] {stage} — awaiting human input...", flush=True)
    prompt = _load_agent_prompt("communicator.md")
    injection = _build_injection(
        today=state.get("today"),
        mode="checkpoint",
        checkpoint_stage=stage,
        agent_packet=json.dumps(_get_latest_packet(state)),
        project_context=state.get("project_context", ""),
    )
    # Format the checkpoint message for the human
    _run_agent_raw(prompt, injection)  # Prints human-readable text, not JSON

    # LangGraph interrupt — execution pauses here
    human_decision = interrupt(
        {
            "checkpoint_type": state.get("checkpoint_type"),
            "stage": state.get("checkpoint_stage"),
            "options": ["proceed", "pause", "redirect"],
        }
    )

    return {"human_decision": human_decision}


def communicator_outbound(state: AgentSystemState) -> AgentSystemState:
    """Format final output for the human and mark workflow complete."""
    print("\n📤 [Communicator] Formatting output...", flush=True)
    prompt = _load_agent_prompt("communicator.md")
    # If this is a synthesis run, surface the artifact path explicitly
    artifact_path = state.get("thinker_packet", {}).get("artifact_path")
    injection = _build_injection(
        today=state.get("today"),
        mode="outbound",
        agent_packet=json.dumps(_get_latest_packet(state)),
        agent_log_ref=_get_latest_log_ref(state),
        artifact_path=artifact_path,  # None → skipped by _build_injection
        project_context=state.get("project_context", ""),
    )
    _run_agent_raw(prompt, injection, AGENT_MODELS["communicator"])  # Prints human-readable text
    print("\n✅ [System] Workflow complete.\n", flush=True)
    return {"completed": True}


# ─────────────────────────────────────────────
# Routing Functions
# ─────────────────────────────────────────────

def route_after_communicator_inbound(state: AgentSystemState) -> str:
    if not state.get("ready_to_proceed", True):
        return END  # Wait for human clarification response
    return "thinker"


def route_after_thinker(state: AgentSystemState) -> str:
    if state.get("thinker_status") == "needs_human_input":
        return "human_checkpoint"
    if state.get("thinker_status") == "blocked":
        return "communicator_outbound"
    if state.get("thinker_status") == "synthesized":
        return "communicator_outbound"

    plan = state.get("thinker_plan", [])
    task_type = state.get("task_type", "project_work")
    assigned_roles = [step.get("assigned_to") for step in plan if step.get("assigned_to")]

    # Debug: show plan breakdown so routing decisions are visible
    print(f"\n[ROUTE] plan steps ({len(plan)}): {assigned_roles}", flush=True)

    # Filter out self-referential "thinker" steps — they are synthesis placeholders,
    # not real agent assignments. Routing should be based on the OTHER agents in the plan.
    external_roles = [r for r in assigned_roles if r != "thinker"]

    has_researcher = any(r == "researcher" for r in external_roles)
    has_dev_work = any(r == "developer" for r in external_roles)

    # Safety net: if the plan has ANY researcher steps and NO developer steps,
    # route to Researcher first — regardless of whether LE steps are also present.
    # This covers hybrid plans (researcher + lead_engineer) where research must
    # complete before LE can meaningfully decompose implementation tasks.
    if has_researcher and not has_dev_work:
        return "researcher"

    # Pure researcher plan (already covered above, kept for clarity)
    if external_roles and all(role == "researcher" for role in external_roles):
        return "researcher"

    # Critic only when there's actual implementation work (developer).
    if has_dev_work:
        return "critic"

    # Strategic / goal / definition tasks with non-trivial plans → Critic
    if task_type in ("strategy", "goal_setting", "project_definition"):
        return "critic"

    # Mixed non-dev work (analysis, reporting, etc.) → skip Critic, go to LE
    return "lead_engineer"


def route_after_critic(state: AgentSystemState) -> str:
    verdict = state.get("critic_verdict")
    retry_count = state.get("thinker_retry_count", 0)

    if verdict in ("revise", "reject"):
        if retry_count >= MAX_THINKER_RETRIES:
            # Hard stop — escalate to human
            return "human_checkpoint"
        # Loop back to Thinker with Critic findings attached in state
        return "thinker"

    # "approved" — proceed to human checkpoint before Lead Engineer
    return "human_checkpoint"  # checkpoint_type = "critic_verdict_review"


def route_after_checkpoint(state: AgentSystemState) -> str:
    decision = state.get("human_decision", "proceed")
    if decision == "pause":
        return "communicator_outbound"
    if decision == "redirect":
        return "communicator_inbound"  # Re-enter with new human input

    # proceed — route based on what triggered the checkpoint
    checkpoint_type = state.get("checkpoint_type")

    if checkpoint_type == "thinker_needs_clarification":
        return "thinker"  # re-run Thinker with human's clarification in prior_output

    if checkpoint_type == "thinker_plan_review":
        return "lead_engineer"
    if checkpoint_type == "critic_verdict_review":
        return "lead_engineer"
    if checkpoint_type == "qa_pass_merge_approval":
        # Check if integration should fire
        batch = state.get("integration_batch", {})
        if batch.get("fire_integration_on_merge"):
            return "integration_agent"
        return "communicator_outbound"
    if checkpoint_type == "integration_failure_review":
        return "lead_engineer"
    if checkpoint_type == "sia_critical_findings":
        return "lead_engineer"
    if checkpoint_type == "destructive_operation":
        return "lead_engineer"

    return "communicator_outbound"


def route_after_lead_engineer(state: AgentSystemState) -> str:
    status = state.get("lead_engineer_status")
    if status == "needs_research":
        return "researcher"
    if status == "needs_human_input":
        return "human_checkpoint"
    if status == "blocked":
        return "communicator_outbound"
    # Research commissioned and non-blocking? Route to developer, research runs parallel
    return "developer"


def route_after_researcher(state: AgentSystemState) -> str:
    """Route Researcher output back to commissioning agent."""
    # Safety cap: prevent infinite research loops
    if state.get("researcher_iteration_count", 0) >= MAX_RESEARCHER_ITERATIONS:
        print(f"\n⚠️  [Route] Researcher iteration cap hit — routing to communicator_outbound", flush=True)
        return "communicator_outbound"
    commissioner = state.get("research_commissioner", "lead_engineer")
    if commissioner == "thinker":
        return "thinker"
    if commissioner == "system_improvement":
        return "system_improvement_agent"
    return "lead_engineer"  # default


def route_after_developer(state: AgentSystemState) -> str:
    status = state.get("developer_status")
    if status == "needs_clarification":
        return "lead_engineer"  # LE responds with clarification_response
    if status == "blocked":
        return "communicator_outbound"
    return "qa"


def route_after_qa(state: AgentSystemState) -> str:
    status = state.get("qa_status")
    task_id = state.get("current_task", {}).get("task_id", "unknown")
    retry_count = state.get("qa_retry_count", {}).get(task_id, 0)

    if status == "fail":
        if retry_count >= MAX_QA_RETRIES:
            # Second failure — escalate to Thinker via Lead Engineer
            return "lead_engineer"
        return "lead_engineer"  # LE re-specs or reissues task

    if status in ("pass", "pass_with_notes"):
        # Check if more tasks remain in queue
        task_queue = state.get("task_queue", [])
        current_task_id = state.get("current_task", {}).get("task_id")
        remaining = [t for t in task_queue if t.get("task_id") != current_task_id]

        if remaining:
            # More tasks to do — QA pass triggers checkpoint for merge approval
            return "human_checkpoint"  # checkpoint_type = "qa_pass_merge_approval"
        # All tasks done
        return "human_checkpoint"  # checkpoint_type = "qa_pass_merge_approval"

    return "communicator_outbound"


def route_after_integration(state: AgentSystemState) -> str:
    status = state.get("integration_status")
    if status == "fail":
        return "human_checkpoint"  # checkpoint_type = "integration_failure_review"
    # pass or pass_with_notes
    return "communicator_outbound"


# ─────────────────────────────────────────────
# Helpers for outbound formatting
# ─────────────────────────────────────────────

def _get_latest_packet(state: AgentSystemState) -> dict:
    """Return the most recently populated agent packet for Communicator."""
    for field in [
        "integration_packet", "qa_packet", "developer_packet",
        "lead_engineer_packet", "critic_packet", "thinker_packet",
        "researcher_packet", "sia_packet",
    ]:
        if state.get(field):
            return state[field]
    return {}


def _get_latest_log_ref(state: AgentSystemState) -> str:
    for field in [
        "integration_log_ref", "qa_log_ref", "developer_log_ref",
        "lead_engineer_log_ref", "critic_log_ref", "thinker_log_ref",
        "researcher_log_ref", "sia_log_ref",
    ]:
        if state.get(field):
            return state[field]
    return ""


# ─────────────────────────────────────────────
# Graph Assembly
# ─────────────────────────────────────────────

def build_graph() -> StateGraph:
    g = StateGraph(AgentSystemState)

    # Nodes
    g.add_node("communicator_inbound", communicator_inbound)
    g.add_node("thinker", thinker)
    g.add_node("critic", critic)
    g.add_node("researcher", researcher)
    g.add_node("lead_engineer", lead_engineer)
    g.add_node("developer", developer)
    g.add_node("qa", qa)
    g.add_node("integration_agent", integration_agent)
    g.add_node("system_improvement_agent", system_improvement_agent)
    g.add_node("human_checkpoint", human_checkpoint)
    g.add_node("communicator_outbound", communicator_outbound)

    # Entry point
    g.set_entry_point("communicator_inbound")

    # Edges
    g.add_conditional_edges("communicator_inbound", route_after_communicator_inbound)
    g.add_conditional_edges("thinker", route_after_thinker)
    g.add_conditional_edges("critic", route_after_critic)
    g.add_conditional_edges("human_checkpoint", route_after_checkpoint)
    g.add_conditional_edges("lead_engineer", route_after_lead_engineer)
    g.add_conditional_edges("researcher", route_after_researcher)
    g.add_conditional_edges("developer", route_after_developer)
    g.add_conditional_edges("qa", route_after_qa)
    g.add_conditional_edges("integration_agent", route_after_integration)

    # Terminal
    g.add_edge("communicator_outbound", END)
    g.add_edge("system_improvement_agent", "communicator_outbound")

    return g


# Compile with MemorySaver — required for interrupt() to work inside nodes
graph = build_graph()
app = graph.compile(checkpointer=MemorySaver())


# ─────────────────────────────────────────────
# Entry point for direct invocation
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import datetime

    human_message = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else input("Task: ")

    initial_state: AgentSystemState = {
        "today": datetime.date.today().isoformat(),
        "human_input": human_message,
        "project_context": "",
        "thinker_retry_count": 0,
        "qa_retry_count": {},
        "pending_research": [],
        "prior_qa_failures": [],
        "completed": False,
        "logs_directory": "./logs",
        "progress_file": "./project/progress.md",
        "adl_file": "./project/adr_log.md",
        "tech_stack_file": "./project/tech_stack.md",
        "sources_file": str(AGENTS_DIR / "sources.yaml"),
        "decision_journal": "./improvement/decision_journal.json",
        "prior_backlog": "./improvement/backlog.json",
    }

    config = {"configurable": {"thread_id": "main"}}
    print(f"\n\n * Starting agent system with: {human_message}...\n\n")

    app.invoke(initial_state, config=config)

    # Interrupt loop — runs until graph reaches END or user stops it
    while True:
        snapshot = app.get_state(config)
        if not snapshot.next:
            # Graph reached END naturally
            break

        # Graph is paused at a checkpoint — get human input and resume
        state_vals = snapshot.values
        checkpoint_type = state_vals.get("checkpoint_type", "")

        print("\n" + "─" * 52, flush=True)

        if checkpoint_type == "thinker_needs_clarification":
            # Show thinker's clarification questions directly
            thinker_pkt = state_vals.get("thinker_packet", {})
            blockers = thinker_pkt.get("blockers", [])
            print("⏸️  CLARIFICATION NEEDED", flush=True)
            if blockers:
                print("", flush=True)
                for b in blockers:
                    print(f"  • {b}", flush=True)
            print("\nAnswer the above (or type 'proceed' to let the system assume):", flush=True)
        else:
            print(f"⏸️  CHECKPOINT: {state_vals.get('checkpoint_stage', 'unknown')}", flush=True)
            print("Options: proceed / pause / redirect", flush=True)

        print("─" * 52, flush=True)
        human_response = input("> ").strip() or "proceed"
        app.invoke(Command(resume=human_response), config=config)

    print("\n✅ Workflow complete.\n", flush=True)
