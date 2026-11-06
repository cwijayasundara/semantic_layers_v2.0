"""The AutoRCA agent's seven specialists. Each is a DeepAgents SubAgent with its own prompt
(prompts/<name>.md) and its own slice of the graph tools, and records what it concludes as
ops:Findings in its own findings graph, which the commander reads back."""
from datetime import datetime
from pathlib import Path

from deepagents import SubAgent

from e2ekg.agent.tools import make_tools
from e2ekg.graph.store import Store

PROMPTS = Path(__file__).parent / "prompts"

SPECIALISTS: dict[str, list[str]] = {
    "topology": ["dependency_closure", "trace_edges"],
    "change": ["changes_in_window"],
    "metrics": ["anomalies_in_window"],
    "logs": ["log_spikes", "raw_evidence"],
    "traces": ["trace_edges", "raw_evidence"],
    "alerts_incidents": ["find_symptom", "alerts_incidents"],
    "history": ["similar_incidents", "chat_claims"],
}

DESCRIPTIONS = {
    "topology": "Maps what the alerting application depends on and which call paths degraded.",
    "change": "Lists every change (deploys, config, experiments, releases, scaling) in a window.",
    "metrics": "Finds which applications' metrics went abnormal, and in what order.",
    "logs": "Finds error log bursts, what they say, and which application they blame.",
    "traces": "Finds slow or failed service-to-service calls and where along the path they began.",
    "alerts_incidents": "Establishes what was observed and when: the alert, its incident, "
                        "and earlier related alerts.",
    "history": "Reads past similar incidents and what responders said in the incident channel.",
}


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text()


def build_specialists(store: Store, proc_root: Path, scenario: str, *, llm=None,
                      as_of: datetime | None = None) -> list[SubAgent]:
    """One SubAgent per specialist, over `store` (an as-of store; findings are written to it)."""
    out = []
    for name, names in SPECIALISTS.items():
        tools = make_tools(store, proc_root, scenario, name, llm=llm, as_of=as_of)
        out.append(SubAgent(name=name, description=DESCRIPTIONS[name],
                            system_prompt=prompt(name),
                            tools=[tools[t] for t in [*names, "record_finding"]],
                            response_format=None))
    return out
