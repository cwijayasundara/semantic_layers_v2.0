"""AutoRCA: a DeepAgents commander that dispatches seven specialists over the graph tools,
tests every candidate with `refutation_check`, and returns an RCAReport.

    report = run_rca("radar-09000", "hero")          # reads data/graph/hero/as_of only

The agent sees the AS-OF store (R34) and nothing else: `run_rca` opens
<root>/graph/<scenario>/as_of/store read-only and the agent works on a private in-memory copy,
so the findings it records never reach the store on disk. It never opens the full store or
<root>/raw/_answers."""
import json
from datetime import datetime
from pathlib import Path

from deepagents import (GeneralPurposeSubagentProfile, HarnessProfile, create_deep_agent,
                        register_harness_profile)
from deepagents._models import get_model_identifier, get_model_provider
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from langchain.agents.structured_output import ToolStrategy
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage

from e2ekg.agent.report import RCAReport
from e2ekg.agent.specialists import build_specialists, prompt
from e2ekg.agent.tools import make_tools
from e2ekg.graph.store import Store
from e2ekg.llm import MODEL_ID

COMMANDER_TOOLS = ("find_symptom", "causal_chains", "refutation_check", "read_findings",
                   "raw_evidence", "sparql_select")
RECURSION_LIMIT = 200


def _private_copy(store: Store) -> Store:
    work = Store(None)
    work.oxigraph.extend(store.oxigraph)
    return work


def _seven_specialists_only(chat: BaseChatModel) -> None:
    """DeepAgents adds a general-purpose subagent and a shell tool unless the model's harness
    profile turns them off; the profile registry is keyed by provider:model, so this keys
    it the way deepagents looks it up."""
    register_harness_profile(
        f"{get_model_provider(chat)}:{get_model_identifier(chat)}",
        HarnessProfile(excluded_tools=frozenset({"execute"}),
                       general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)))


# ToolStrategy, not the provider's response_format: with a JSON schema bound, GLM-5.3-flash
# wrote a report at once, with no tool call (live run 1).
def build_autorca(store: Store, proc_root: Path, scenario: str,
                  model: str | BaseChatModel = f"fireworks:{MODEL_ID}", *, llm=None,
                  as_of: datetime | None = None, evidence_root: Path | None = None):
    """The compiled agent over a private copy of `store` (the as-of store). `llm` lets the
    tools read nicknames; `as_of` is the store's cutoff; `/evidence/` maps to `evidence_root`
    (default <root>/agent/<scenario> beside <root>/processed/<scenario>)."""
    work = _private_copy(store)
    chat = init_chat_model(model) if isinstance(model, str) else model
    _seven_specialists_only(chat)
    tools = make_tools(work, proc_root, scenario, "autorca", llm=llm, as_of=as_of)
    evidence = Path(evidence_root) if evidence_root else \
        Path(proc_root).absolute().parent.parent / "agent" / scenario
    evidence.mkdir(parents=True, exist_ok=True)
    return create_deep_agent(
        model=chat, tools=[tools[n] for n in COMMANDER_TOOLS], system_prompt=prompt("autorca"),
        subagents=build_specialists(work, proc_root, scenario, llm=llm, as_of=as_of),
        response_format=ToolStrategy(RCAReport), name="autorca",
        backend=CompositeBackend(default=StateBackend(), routes={
            "/evidence/": FilesystemBackend(root_dir=evidence, virtual_mode=True)}))


def run_rca(alert_id: str, scenario: str, *, root: Path = Path("data"),
            model: str | BaseChatModel = f"fireworks:{MODEL_ID}", llm=None,
            evidence_root: Path | None = None, callbacks: list | None = None) -> RCAReport:
    """Investigate `alert_id` over <root>/graph/<scenario>/as_of. `llm` defaults to the
    cached Fireworks client (the Resolver's nickname reader)."""
    graph = Path(root) / "graph" / scenario / "as_of"
    as_of = datetime.fromisoformat(json.loads((graph / "harvest.json").read_text())["as_of"])
    if llm is None:
        from e2ekg.llm import FireworksClient
        llm = FireworksClient()
    agent = build_autorca(Store(graph / "store", read_only=True), Path(root) / "processed" / scenario,
                          scenario, model, llm=llm, as_of=as_of, evidence_root=evidence_root)
    out = agent.invoke(
        {"messages": [HumanMessage(f"Alert {alert_id} fired. Find its root cause and return "
                                   "the RCAReport.")]},
        config={"recursion_limit": RECURSION_LIMIT, "callbacks": callbacks or []})
    return out["structured_response"]
