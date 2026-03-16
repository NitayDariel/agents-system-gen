from graph import _run_agent, _load_agent_prompt, _build_injection
import datetime

prompt = _load_agent_prompt("communicator.md")
injection = _build_injection(
    today=datetime.date.today().isoformat(),
    mode="inbound",
    human_input="I want to build a simple REST API",
    project_context=""
)
packet = _run_agent(prompt, injection)
print(packet)
