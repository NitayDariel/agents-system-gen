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
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from langgraph.graph import StateGraph, END
from langgraph.types import Command, interrupt
from langgraph.checkpoint.sqlite import SqliteSaver

from state import AgentSystemState
import ui


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

AGENTS_DIR = Path(__file__).parent
TELOS_PATH = Path.home() / ".claude/skills/PAI/USER/TELOS/goals.md"
MAX_THINKER_RETRIES = 3
MAX_QA_RETRIES = 2
MAX_RESEARCHER_ITERATIONS = 12  # Each queue commission counts as 1 iteration
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
    ui.console.out(result.stdout.strip())


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
        ui.error(f"returncode: {result.returncode}")
        if result.stderr:
            ui.error(f"stderr: {result.stderr[:400]}")
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
        ui.packet_tree(packet)

    return packet


def _build_injection(**kwargs) -> str:
    """Build the runtime injection block passed to each agent."""
    lines = []
    for key, value in kwargs.items():
        if value is not None:
            lines.append(f"{key.upper()}: {value}")
    return "\n".join(lines)


def _format_agent_flow(flow: list[str]) -> str:
    """Collapse consecutive duplicates and return a compact chain string.

    e.g. ["comm","clarify","thinker","researcher","researcher","researcher","output"]
    →    "[comm] → [clarify] → [thinker] → [researcher×3] → [output]"
    """
    if not flow:
        return "(empty)"
    collapsed = []
    i = 0
    while i < len(flow):
        label = flow[i]
        count = 1
        while i + count < len(flow) and flow[i + count] == label:
            count += 1
        collapsed.append(f"{label}×{count}" if count > 1 else label)
        i += count
    return " → ".join(f"[{s}]" for s in collapsed)


# ─────────────────────────────────────────────
# Nodes
# ─────────────────────────────────────────────

def communicator_inbound(state: AgentSystemState) -> AgentSystemState:
    """Parse and classify human input. Produce structured task packet."""

    # ── Deterministic resume path: skip LLM entirely ──────────────────────
    # _run_interrupt_loop writes resolved_clarification to state via app.update_state()
    # before calling Command(resume=...). On the resume run, LangGraph re-executes
    # this function from the top — we detect the resolved answer here and return
    # immediately without any LLM call, eliminating the double-ask bug entirely.
    resolved = state.get("resolved_clarification")
    if resolved:
        original_input = state.get("human_input", "")
        task_str = f"{original_input} — clarification criterion: {resolved}"
        ui.agent_header("📨", "Communicator", detail="clarification resolved — no LLM")
        ui.agent_result("complete", f"{state.get('task_type', 'project_work')} — {task_str}")
        return {
            "communicator_task": task_str,
            "task_type": state.get("task_type", "project_work"),
            "telos_required": state.get("telos_required", False),
            "clarification_asked": False,
            "clarification_question": None,
            "ready_to_proceed": True,
            "resolved_clarification": None,  # clear after use
            "agent_flow": state.get("agent_flow", []) + ["clarify"],
        }

    # ── Normal path: call LLM ─────────────────────────────────────────────
    ui.agent_header("📨", "Communicator", AGENT_MODELS["communicator"])
    prompt = _load_agent_prompt("communicator.md")
    injection = _build_injection(
        today=state.get("today"),
        mode="inbound",
        human_input=state.get("human_input"),
        project_context=state.get("project_context", ""),
    )
    with ui.spinner("Parsing input..."):
        packet = _run_agent(prompt, injection, AGENT_MODELS["communicator"])
    ui.agent_result(packet.get("task_type", "?"), packet.get("task", ""))
    ui.log_link(packet.get("log_ref", ""))

    # ── Early clarification: pause BEFORE expensive downstream calls ───────
    if packet.get("clarification_asked") and not packet.get("ready_to_proceed", True):
        question = packet.get("clarification_question", "Please clarify your request.")
        interrupt({"question": question, "stage": "clarification"})
        # Execution never continues past here on the first run (interrupt pauses it).
        # On resume, resolved_clarification is in state → the early-exit above handles it.

    updates: AgentSystemState = {
        "communicator_task": packet.get("task", ""),
        "task_type": packet.get("task_type", "project_work"),
        "telos_required": packet.get("telos_required", False),
        "clarification_asked": packet.get("clarification_asked", False),
        "clarification_question": packet.get("clarification_question"),
        "ready_to_proceed": packet.get("ready_to_proceed", True),
        "agent_flow": state.get("agent_flow", []) + ["comm"],
    }
    if packet.get("telos_required"):
        updates["telos_source_path"] = packet.get("telos_source_path", str(TELOS_PATH))

    return updates


def thinker(state: AgentSystemState) -> AgentSystemState:
    """Produce a reasoned plan. Applies reality anchor, inversion, base rate."""
    retry = state.get("thinker_retry_count", 0)
    retry_tag = f"retry {retry}" if retry > 0 else ""
    ui.agent_header("🧠", "Thinker", AGENT_MODELS["thinker"], retry_tag)
    prompt = _load_agent_prompt("thinker_v2-2.md")

    # Load TELOS only when required by Communicator
    telos_content = ""
    if state.get("telos_required"):
        telos_path = state.get("telos_source_path", str(TELOS_PATH))
        telos_content = Path(telos_path).read_text()

    # PRIOR_OUTPUT: inject human clarification, researcher findings, or Critic summary
    # in priority order.
    prior_output = ""
    if state.get("checkpoint_type") in ("thinker_needs_clarification", "synthesis_needs_clarification") and state.get("human_decision"):
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
    with ui.spinner("Planning..."):
        packet = _run_agent(prompt, injection, AGENT_MODELS["thinker"])
    ui.agent_result(packet.get("status", "?"), packet.get("summary", ""))
    ui.log_link(packet.get("log_ref", ""))

    plan = packet.get("plan", [])

    # Extract ALL researcher commissions from plan, load as a queue
    all_research_steps = [
        s for s in plan
        if s.get("assigned_to") == "researcher" and s.get("researcher_commission")
    ]

    updates: AgentSystemState = {
        "agent_flow": state.get("agent_flow", []) + ["thinker"],
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
        # Reset researcher accumulation for new planning cycle
        "researcher_findings": [],
        "researcher_iteration_count": 0,
        "researcher_has_more": False,
    }
    if packet.get("status") == "needs_human_input":
        updates["checkpoint_type"] = "thinker_needs_clarification"
        updates["checkpoint_stage"] = "thinker_needs_clarification"
        updates["checkpoint_required"] = True
    if packet.get("status") == "synthesized" and packet.get("blockers"):
        # Synthesis is incomplete — needs human clarification before finishing
        updates["checkpoint_type"] = "synthesis_needs_clarification"
        updates["checkpoint_stage"] = "synthesis_needs_clarification"
        updates["checkpoint_required"] = True
    if all_research_steps:
        # Load first commission, queue the rest for sequential execution
        updates["research_commission"] = all_research_steps[0]["researcher_commission"]
        updates["pending_research"] = [s["researcher_commission"] for s in all_research_steps[1:]]
        ui.info(f"📋 Research queue: {len(all_research_steps)} commission(s) to run")

    return updates


def critic(state: AgentSystemState) -> AgentSystemState:
    """Adversarially review the Thinker's plan. Can reject or approve."""
    ui.agent_header("⚖️ ", "Critic", AGENT_MODELS["critic"])
    prompt = _load_agent_prompt("critic.md")
    injection = _build_injection(
        today=state.get("today"),
        task=state.get("communicator_task"),
        task_type=state.get("task_type"),
        project_context=state.get("project_context", ""),
        thinker_packet=json.dumps(state.get("thinker_packet", {})),
        thinker_log_ref=state.get("thinker_log_ref"),
    )
    with ui.spinner("Reviewing plan..."):
        packet = _run_agent(prompt, injection, AGENT_MODELS["critic"])
    ui.agent_result(packet.get("verdict", "?"), packet.get("summary", ""))
    ui.log_link(packet.get("log_ref", ""))

    verdict = packet.get("verdict", "approved")
    # Increment here so route_after_critic reads the updated count (not pre-thinker count)
    new_retry_count = state.get("thinker_retry_count", 0) + (1 if verdict in ("revise", "reject") else 0)

    return {
        "agent_flow": state.get("agent_flow", []) + ["critic"],
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
    ui.agent_header("🔬", "Researcher", AGENT_MODELS["researcher"], q[:60] + ("…" if len(q) > 60 else ""))
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
    with ui.spinner("Searching..."):
        packet = _run_agent(prompt, injection, AGENT_MODELS["researcher"])
    n = len(packet.get("findings", []))
    ui.agent_result(packet.get("status", "?"), f"{n} finding(s). {packet.get('summary', '')}")
    ui.log_link(packet.get("log_ref", ""))

    # Accumulate findings across all commissions (don't overwrite)
    existing_findings = state.get("researcher_findings", [])
    new_findings = packet.get("findings", packet.get("key_findings", []))
    accumulated = existing_findings + new_findings

    # Pop next commission from queue if available
    pending = list(state.get("pending_research", []))
    has_more = bool(pending)
    next_commission = pending.pop(0) if has_more else None

    updates: AgentSystemState = {
        "agent_flow": state.get("agent_flow", []) + ["researcher"],
        "researcher_status": packet.get("status", "complete"),
        "researcher_packet": packet,
        "researcher_findings": accumulated,
        "researcher_log_ref": packet.get("log_ref", ""),
        "researcher_iteration_count": state.get("researcher_iteration_count", 0) + 1,
        "pending_research": pending,
        "researcher_has_more": has_more,
    }
    if has_more:
        updates["research_commission"] = next_commission
        ui.info(f"🔁 {len(pending)} commission(s) remaining in queue")
    return updates


def lead_engineer(state: AgentSystemState) -> AgentSystemState:
    """Decompose plan into tasks with DoD. Governs git. Handles QA failures."""
    ui.agent_header("🔧", "Lead Engineer", AGENT_MODELS.get("lead_engineer", ""))
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
    with ui.spinner("Decomposing tasks..."):
        packet = _run_agent(prompt, injection, AGENT_MODELS["lead_engineer"])
    n = len(packet.get("tasks", []))
    ui.agent_result(packet.get("status", "?"), f"{n} task(s) defined. {packet.get('summary', '')}")
    ui.log_link(packet.get("log_ref", ""))

    tasks = packet.get("tasks", [])
    return {
        "agent_flow": state.get("agent_flow", []) + ["lead_eng"],
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
    ui.agent_header("💻", "Developer", AGENT_MODELS.get("developer", ""), f"{task_id}: {title}")
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
    with ui.spinner(f"Implementing {task_id}..."):
        packet = _run_agent(prompt, injection, AGENT_MODELS["developer"])
    ui.agent_result(packet.get("status", "?"), packet.get("summary", ""))
    ui.log_link(packet.get("log_ref", ""))

    updates: AgentSystemState = {
        "agent_flow": state.get("agent_flow", []) + ["dev"],
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
    ui.agent_header("🔍", "QA", AGENT_MODELS.get("qa", ""), task_id)
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
    with ui.spinner(f"Verifying {task_id}..."):
        packet = _run_agent(prompt, injection, AGENT_MODELS["qa"])
    status = packet.get("status", "?")
    n_fail = len([f for f in packet.get("findings", []) if f.get("severity") in ("p0", "p1")])
    ui.agent_result(status, f"{n_fail} blocking finding(s). {packet.get('summary', '')}")
    ui.log_link(packet.get("log_ref", ""))

    # Track per-task retry count
    task_id = task.get("task_id", "unknown")
    qa_retry_count = dict(state.get("qa_retry_count", {}))
    qa_status = packet.get("status", "pass")
    if qa_status == "fail":
        qa_retry_count[task_id] = qa_retry_count.get(task_id, 0) + 1

    return {
        "agent_flow": state.get("agent_flow", []) + ["qa"],
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
    ui.agent_header("🔗", "Integration", AGENT_MODELS.get("integration_agent", ""), f"batch {batch_id}")
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
    ui.agent_result(packet.get("status", "?"), packet.get("summary", ""))
    log_ref = packet.get("log_ref", "")
    ui.log_link(log_ref)
    return {
        "agent_flow": state.get("agent_flow", []) + ["integration"],
        "integration_status": packet.get("status", "pass"),
        "integration_packet": packet,
        "integration_log_ref": log_ref,
        "prior_integration_log": log_ref,
    }


def system_improvement_agent(state: AgentSystemState) -> AgentSystemState:
    """Weekly meta-audit. Reads all agent logs and produces improvement backlog."""
    ui.agent_header("🔄", "SIA", AGENT_MODELS.get("system_improvement_agent", ""), state.get("sia_run_type", "scheduled"))
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
    with ui.spinner("SIA auditing system logs..."):
        packet = _run_agent(prompt, injection, AGENT_MODELS["system_improvement_agent"])
    ui.agent_result(packet.get("system_health", "?"), packet.get("summary", ""))
    ui.log_link(packet.get("log_ref", ""))

    return {
        "agent_flow": state.get("agent_flow", []) + ["sia"],
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
    ui.agent_header("⏸️", "Checkpoint", detail=stage)
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

    return {
        "human_decision": human_decision,
        "agent_flow": state.get("agent_flow", []) + ["checkpoint"],
    }


def communicator_outbound(state: AgentSystemState) -> AgentSystemState:
    """Format final output for the human and mark workflow complete."""
    ui.agent_header("📤", "Communicator", AGENT_MODELS["communicator"], "outbound")
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
    flow = state.get("agent_flow", []) + ["output"]
    ui.flow_summary(flow)
    return {"completed": True, "agent_flow": flow}


# ─────────────────────────────────────────────
# Routing Functions
# ─────────────────────────────────────────────

def route_after_communicator_inbound(state: AgentSystemState) -> str:
    # Clarification is now handled inside communicator_inbound via interrupt().
    # By the time we reach this router, ready_to_proceed is always True.
    return "thinker"


def route_after_thinker(state: AgentSystemState) -> str:
    if state.get("thinker_status") == "needs_human_input":
        return "human_checkpoint"
    if state.get("thinker_status") == "blocked":
        return "communicator_outbound"
    if state.get("thinker_status") == "synthesized":
        # If synthesis has unresolved blockers, pause for human clarification
        if state.get("thinker_packet", {}).get("blockers"):
            return "human_checkpoint"
        return "communicator_outbound"

    plan = state.get("thinker_plan", [])
    task_type = state.get("task_type", "project_work")
    assigned_roles = [step.get("assigned_to") for step in plan if step.get("assigned_to")]

    # Debug: show plan breakdown so routing decisions are visible
    ui.info(f"[ROUTE] plan steps ({len(plan)}): {assigned_roles}")

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

    if checkpoint_type == "synthesis_needs_clarification":
        return "thinker"  # re-run Thinker (synthesis mode) with human's answers

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
        ui.info("⚠  [Route] Researcher iteration cap hit — routing to communicator_outbound")
        return "communicator_outbound"
    # More commissions queued — loop back to run next one
    if state.get("researcher_has_more"):
        ui.info("[Route] More research commissions queued — continuing researcher loop")
        return "researcher"
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


CHECKPOINT_DB = str(AGENTS_DIR / "checkpoint.db")


def build_app(checkpointer):
    """Compile the graph with a given checkpointer. Called at runtime, not import time."""
    return build_graph().compile(checkpointer=checkpointer)


# ─────────────────────────────────────────────
# Entry point for direct invocation
# ─────────────────────────────────────────────

def _resolve_option_letters(human_input: str, question_text: str) -> str:
    """
    If the human replied with letter(s) like 'c', 'a & d', 'b, c', etc.,
    look up each letter in the option list from `question_text` and return
    the full resolved text. This makes resume values deterministic — the LLM
    never needs to re-interpret shorthand.

    If the input is free text (not a letter pattern), return it unchanged.
    """
    import re

    # Detect if the whole response is letter-based: single letters separated by
    # spaces, commas, &, 'and', or 'or'. e.g. "c", "a & d", "b, c", "a and b"
    letter_pattern = re.compile(
        r"^(?:[a-zA-Z](?:\s*(?:,|&|\band\b|\bor\b)\s*[a-zA-Z])*)\s*$",
        re.IGNORECASE,
    )
    if not letter_pattern.match(human_input.strip()):
        return human_input  # free-text answer — pass through unchanged

    # Extract options from the question text: patterns like "(a) text" or "a) text"
    options = {}
    for match in re.finditer(r"\(([a-zA-Z])\)\s*([^,()\n]+?)(?=\s*[,(]|\s*—|\s*$)", question_text):
        letter = match.group(1).lower()
        text = match.group(2).strip().rstrip(" —,")
        if text:
            options[letter] = text

    if not options:
        return human_input  # couldn't parse options — pass through unchanged

    # Extract individual STANDALONE letters the human chose.
    # Split on whitespace/commas/& and keep only single-char tokens (skip "and", "or", etc.)
    tokens = re.split(r"[\s,&]+", human_input.strip())
    chosen_letters = [t.lower() for t in tokens if len(t) == 1 and t.isalpha()]
    resolved = []
    unresolved = []
    for letter in chosen_letters:
        if letter in options:
            resolved.append(options[letter])
        else:
            unresolved.append(letter)

    if not resolved:
        return human_input  # no matches — pass through unchanged

    result = " AND ".join(resolved)
    if unresolved:
        result += f" (also mentioned: {', '.join(unresolved)})"
    return result


def _handle_explore_command(cmd: str, app, config: dict) -> None:
    """
    Process a live exploration command typed while agents are running.
    Reads current graph state and prints results to the scrollback area
    above the live status panel. Does NOT block the background thread.
    """
    from rich.panel import Panel as _Panel

    snapshot = app.get_state(config)
    state = snapshot.values if snapshot else {}

    findings = (state.get("researcher_packet") or {}).get("findings", [])
    plan = (state.get("thinker_packet") or {}).get("plan", [])
    pending = state.get("pending_research") or []
    flow = state.get("agent_flow") or []
    assumptions = (state.get("thinker_packet") or {}).get("open_assumptions", [])

    cmd = cmd.strip().lower()

    if cmd in ("help", "?", "h"):
        ui.console.print(
            "[dim]Commands: findings · plan · flow · queue · assumptions · "
            "f1-fN · p1-pN · state[/dim]"
        )
    elif cmd == "findings":
        if not findings:
            ui.console.print("[dim]No findings yet.[/dim]")
        else:
            for i, f in enumerate(findings, 1):
                ct = f.get("claim_type", "")
                cl = f.get("claim", str(f))[:120]
                ui.console.print(f"  [dim]{i}.[/dim] [blue]{ct}[/blue]  {cl}")
    elif cmd == "plan":
        if not plan:
            ui.console.print("[dim]No plan yet.[/dim]")
        else:
            for i, step in enumerate(plan, 1):
                desc = step.get("description", str(step))[:120]
                agent = step.get("assigned_to", "")
                ui.console.print(f"  [dim]{i}.[/dim] [magenta]{agent}[/magenta]  {desc}")
    elif cmd == "flow":
        if not flow:
            ui.console.print("[dim]No agent flow yet.[/dim]")
        else:
            ui.console.print("  " + " → ".join(flow))
    elif cmd == "queue":
        if not pending:
            ui.console.print("[dim]No pending research.[/dim]")
        else:
            ui.console.print(f"  [dim]{len(pending)} commission(s) queued[/dim]")
            for i, p in enumerate(pending, 1):
                label = p.get("commission_label", p.get("description", str(p)))[:100]
                ui.console.print(f"  [dim]{i}.[/dim] {label}")
    elif cmd == "assumptions":
        if not assumptions:
            ui.console.print("[dim]No open assumptions.[/dim]")
        else:
            for i, a in enumerate(assumptions, 1):
                ui.console.print(f"  [dim]{i}.[/dim] {str(a)[:120]}")
    elif cmd == "state":
        keys = ["thinker_status", "researcher_status", "critic_status",
                "lead_engineer_status", "developer_status", "qa_status"]
        for k in keys:
            v = state.get(k)
            if v:
                ui.console.print(f"  [dim]{k}:[/dim] {v}")
    elif cmd.startswith("f") and cmd[1:].isdigit():
        idx = int(cmd[1:]) - 1
        if 0 <= idx < len(findings):
            f = findings[idx]
            lines = "\n".join(
                f"  [dim]{k}:[/dim]  {str(v)[:200]}"
                for k, v in f.items()
            )
            ui.console.print(_Panel(lines, title=f"[blue]Finding {idx+1} of {len(findings)}[/blue]",
                                    border_style="dim blue"))
        else:
            ui.console.print(f"[dim]Finding {idx+1} not found (have {len(findings)})[/dim]")
    elif cmd.startswith("p") and cmd[1:].isdigit():
        idx = int(cmd[1:]) - 1
        if 0 <= idx < len(plan):
            step = plan[idx]
            lines = "\n".join(
                f"  [dim]{k}:[/dim]  {str(v)[:200]}"
                for k, v in step.items()
            )
            ui.console.print(_Panel(lines, title=f"[magenta]Plan Step {idx+1} of {len(plan)}[/magenta]",
                                    border_style="dim magenta"))
        else:
            ui.console.print(f"[dim]Step {idx+1} not found (have {len(plan)})[/dim]")
    else:
        ui.console.print(f"[dim]Unknown: {cmd!r}. Type help for commands.[/dim]")


def _handle_interrupt(app, config: dict, snapshot) -> str:
    """
    Render the appropriate panel for an interrupt, run the inspect REPL,
    and return the human response string.

    Called from the main thread while the graph thread is blocked.
    """
    state_vals = snapshot.values
    checkpoint_type = state_vals.get("checkpoint_type", "")

    # Check if this pause is a communicator clarification (early, pre-Thinker)
    clarification_interrupt = None
    for task in snapshot.tasks:
        for irpt in getattr(task, "interrupts", []):
            val = getattr(irpt, "value", None)
            if isinstance(val, dict) and val.get("stage") == "clarification":
                clarification_interrupt = val
                break

    if clarification_interrupt:
        ui.clarification_panel(clarification_interrupt.get("question", ""))
    elif checkpoint_type == "thinker_needs_clarification":
        thinker_pkt = state_vals.get("thinker_packet", {})
        blockers = thinker_pkt.get("blockers", [])
        ui.checkpoint_panel(
            "Thinker needs clarification",
            blockers=blockers,
            prompt_text="Answer the above (or 'proceed' to let the system assume):",
        )
    elif checkpoint_type == "synthesis_needs_clarification":
        thinker_pkt = state_vals.get("thinker_packet", {})
        blockers = thinker_pkt.get("blockers", [])
        ui.checkpoint_panel(
            "Synthesis — open questions",
            blockers=blockers,
            prompt_text="Answer the above (or 'proceed' to synthesize with best assumptions):",
        )
    else:
        ui.checkpoint_panel(
            state_vals.get("checkpoint_stage", "unknown"),
            options=["proceed", "pause", "redirect"],
        )

    # Inspect REPL — user can drill into findings/plan before responding
    human_response = ui.inspect_repl(state_vals) or "proceed"

    if clarification_interrupt:
        human_response = _resolve_option_letters(
            human_response,
            clarification_interrupt.get("question", ""),
        )
        app.update_state(config, {"resolved_clarification": human_response})

    return human_response


def _graph_worker(app, config: dict, event_q: "queue.Queue", response_q: "queue.Queue") -> None:
    """
    Background thread: drives the LangGraph graph to completion.

    Protocol:
      - Runs app.invoke() until the graph has no pending nodes
      - On each interrupt: puts snapshot onto event_q, blocks on response_q
      - On completion: puts None onto event_q and exits
    """
    cmd = None  # first invoke uses initial state (already invoked before thread starts)
    while True:
        snapshot = app.get_state(config)
        if not snapshot.next:
            event_q.put(None)  # signal completion
            return
        # Signal interrupt to main thread
        event_q.put(snapshot)
        # Block until main thread sends response
        human_response = response_q.get()
        # Resume graph
        app.invoke(Command(resume=human_response), config=config)


def _run_interrupt_loop(app, config: dict, thread_id: str) -> None:
    """
    Handle the interactive checkpoint loop until the graph reaches END.

    When ui.LIVE_DISPLAY is True: runs graph on a background thread, main thread
    manages the live status panel and handles interrupt prompts.
    When ui.LIVE_DISPLAY is False: runs graph synchronously (Phase 2 behaviour).
    """
    if not ui.LIVE_DISPLAY:
        # ── Synchronous fallback (Phase 2 behaviour) ─────────────────────
        while True:
            snapshot = app.get_state(config)
            if not snapshot.next:
                break
            human_response = _handle_interrupt(app, config, snapshot)
            app.invoke(Command(resume=human_response), config=config)
        ui.run_complete(thread_id)
        return

    # ── Two-thread model ─────────────────────────────────────────────────
    # Check if graph is already done (no pending nodes)
    snapshot = app.get_state(config)
    if not snapshot.next:
        ui.run_complete(thread_id)
        return

    event_q: queue.Queue = queue.Queue()
    response_q: queue.Queue = queue.Queue()

    # Start graph worker thread
    worker = threading.Thread(
        target=_graph_worker,
        args=(app, config, event_q, response_q),
        daemon=True,
    )
    worker.start()

    import select
    import sys

    try:
        while True:
            # Idle loop: refresh live panel + poll stdin for explore commands
            while event_q.empty():
                if ui._live is not None:
                    ui._live.update(ui._make_status_panel())
                # Non-blocking stdin check — lets users explore state while agents run
                if sys.stdin.isatty():
                    r, _, _ = select.select([sys.stdin], [], [], 0.1)
                    if r:
                        ui.live_pause()
                        line = sys.stdin.readline().strip()
                        if line:
                            _handle_explore_command(line, app, config)
                        ui.live_resume()
                else:
                    time.sleep(0.25)

            event = event_q.get()

            if event is None:
                # Graph completed
                break

            # Interrupt — pause live display, handle interactively, resume
            snapshot = event
            ui.live_pause()
            human_response = _handle_interrupt(app, config, snapshot)
            response_q.put(human_response)
            ui.live_resume()

    finally:
        worker.join(timeout=5)

    ui.run_complete(thread_id)


def _cmd_list(checkpointer) -> None:
    """Print all runs stored in the checkpoint DB with their status."""
    import sqlite3
    try:
        conn = sqlite3.connect(CHECKPOINT_DB)
        rows = conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id"
        ).fetchall()
        conn.close()
    except Exception:
        print("No runs found (checkpoint.db doesn't exist yet).")
        return

    if not rows:
        print("No runs found.")
        return

    app = build_app(checkpointer)
    print(f"\n{'ID':<20} {'Status':<12} {'Waiting on'}", flush=True)
    print("─" * 52, flush=True)
    for (tid,) in rows:
        try:
            snap = app.get_state({"configurable": {"thread_id": tid}})
            if not snap.values:
                status, waiting = "empty", ""
            elif snap.next:
                status = "⏸  paused"
                waiting = snap.values.get("checkpoint_stage", snap.values.get("checkpoint_type", ""))
            else:
                status = "✅ complete"
                waiting = ""
        except Exception:
            status, waiting = "unknown", ""
        print(f"{tid:<20} {status:<12} {waiting}", flush=True)
    print("", flush=True)


_AUTO_ID_WORDS = [
    "fox", "owl", "elk", "hawk", "wolf", "bear", "crow", "lynx", "swan",
    "wren", "crane", "dove", "eagle", "finch", "heron", "lark", "moose",
    "rook", "stork", "viper", "bison", "cobra", "dingo", "ember", "flint",
    "grove", "inlet", "kelp", "lotus", "maple", "north", "orbit", "prism",
    "quartz", "ridge", "solar", "thorn", "umbra", "vault", "whirl",
]


def _thread_exists(thread_id: str) -> bool:
    """Return True if thread_id has any checkpoint data in the DB."""
    import sqlite3
    try:
        conn = sqlite3.connect(CHECKPOINT_DB)
        row = conn.execute(
            "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1", (thread_id,)
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def _generate_auto_id() -> str:
    """Pick a random word-hex ID that doesn't already exist in the DB."""
    import random
    hex_chars = "0123456789abcdef"
    for _ in range(100):  # give up after 100 tries (640 combos, very unlikely to exhaust)
        word = random.choice(_AUTO_ID_WORDS)
        suffix = random.choice(hex_chars)
        candidate = f"{word}-{suffix}"
        if not _thread_exists(candidate):
            return candidate
    # Fallback: plain uuid if somehow all slots taken
    import uuid
    return f"run-{uuid.uuid4().hex[:8]}"


def _cmd_end(thread_id: str) -> None:
    """Delete all checkpoints for a specific run (with confirmation)."""
    import sqlite3
    confirm = input(f"Delete run '{thread_id}'? This cannot be undone. [y/N]: ").strip().lower()
    if confirm != "y":
        print("Cancelled.", flush=True)
        return
    try:
        conn = sqlite3.connect(CHECKPOINT_DB)
        conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
        conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
        conn.commit()
        conn.close()
        print(f"Run '{thread_id}' deleted.", flush=True)
    except Exception as e:
        print(f"Could not delete run '{thread_id}': {e}", flush=True)


def _cmd_end_all() -> None:
    """Delete all checkpoint data (with confirmation)."""
    import sqlite3
    confirm = input("Delete ALL runs? This cannot be undone. [y/N]: ").strip().lower()
    if confirm != "y":
        print("Cancelled.", flush=True)
        return
    try:
        conn = sqlite3.connect(CHECKPOINT_DB)
        conn.execute("DELETE FROM checkpoints")
        conn.execute("DELETE FROM writes")
        conn.commit()
        conn.close()
        print("All runs deleted.", flush=True)
    except Exception as e:
        print(f"Could not clear checkpoint DB: {e}", flush=True)


if __name__ == "__main__":
    import sys
    import argparse
    import datetime

    parser = argparse.ArgumentParser(description="Agent system orchestrator")
    parser.add_argument("task", nargs="?", help="Task description (fresh run) or answer (resume)")
    parser.add_argument("-n", "--name",    metavar="NAME",   help="Name this run (no spaces; must be unique)")
    parser.add_argument("-r", "--resume",  metavar="NAME",   help="Resume a paused run by name")
    parser.add_argument(      "--list",    action="store_true", help="List all runs and their status")
    parser.add_argument(      "--end",     metavar="NAME",   help="Delete a specific run's checkpoint data")
    parser.add_argument(      "--end-all", action="store_true", help="Delete all checkpoint data")
    args = parser.parse_args()

    with SqliteSaver.from_conn_string(CHECKPOINT_DB) as checkpointer:

        # ── --list ──────────────────────────────────────────────
        if args.list:
            _cmd_list(checkpointer)
            sys.exit(0)

        # ── --end / --end-all ────────────────────────────────────
        if args.end:
            _cmd_end(args.end)
            sys.exit(0)
        if args.end_all:
            _cmd_end_all()
            sys.exit(0)

        # ── --resume ─────────────────────────────────────────────
        if args.resume:
            thread_id = args.resume
            config = {"configurable": {"thread_id": thread_id}}
            app = build_app(checkpointer)
            snap = app.get_state(config)
            if not snap.values:
                print(f"Run '{thread_id}' not found. Use --list to see available runs.")
                sys.exit(1)
            if not snap.next:
                if args.task:
                    # Follow-up: start a new run with previous synthesis as project_context
                    prev_synthesis = snap.values.get("thinker_packet", {})
                    prev_summary = prev_synthesis.get("synthesis", prev_synthesis.get("real_question", ""))
                    follow_id = _generate_auto_id()
                    follow_config = {"configurable": {"thread_id": follow_id}}
                    follow_state: AgentSystemState = {
                        "today": datetime.date.today().isoformat(),
                        "human_input": args.task,
                        "project_context": prev_summary,
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
                    ui.run_start(follow_id, f"[Follow-up from: {thread_id}] {args.task}", logs_dir="./logs")
                    follow_app = build_app(checkpointer)
                    follow_app.invoke(follow_state, config=follow_config)
                    _run_interrupt_loop(follow_app, follow_config, follow_id)
                else:
                    print(f"Run '{thread_id}' is already complete. Pass a task to start a follow-up run.")
                sys.exit(0)
            ui.run_start(thread_id, f"[Resume] {args.task or '...'}", logs_dir="./logs")
            if args.task:
                app.invoke(Command(resume=args.task), config=config)
            _run_interrupt_loop(app, config, thread_id)
            sys.exit(0)

        # ── Fresh start ──────────────────────────────────────────
        if not args.task:
            args.task = input("Task: ").strip()

        if args.name:
            if _thread_exists(args.name):
                print(f"Run '{args.name}' already exists. Use -r {args.name} to resume it, or choose a different name.")
                sys.exit(1)
            thread_id = args.name
        else:
            thread_id = _generate_auto_id()

        config = {"configurable": {"thread_id": thread_id}}

        initial_state: AgentSystemState = {
            "today": datetime.date.today().isoformat(),
            "human_input": args.task,
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

        logs_dir = initial_state.get("logs_directory", "./logs")
        ui.run_start(thread_id, args.task, logs_dir=logs_dir)
        app = build_app(checkpointer)
        app.invoke(initial_state, config=config)
        _run_interrupt_loop(app, config, thread_id)
