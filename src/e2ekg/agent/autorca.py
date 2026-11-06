"""AutoRCA: a DeepAgents commander that dispatches seven specialists over the graph tools,
proposes candidate root causes, weighs the facts about each, and returns an RCAReport.

    report = run_rca("radar-09000", "hero")          # reads data/graph/hero/as_of only

The model decides. No tool returns a verdict: `candidate_evidence` reports facts about the
changes the commander names (R50), and `with_evidence_tool=False` builds the same agent
without it (R53's graph_no_evidence_tool arm).

The agent sees the AS-OF store (R34) and nothing else: `run_rca` opens
<root>/graph/<scenario>/as_of/store read-only and the agent works on a private in-memory copy
without any findings graph, so what it records never reaches the store on disk and no earlier
run's findings reach it. It never opens the full store or <root>/raw/_answers. `/evidence/` is
a fresh temporary directory per run unless the caller names one (R52)."""
import json
import tempfile
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
from langgraph.errors import GraphRecursionError

from e2ekg.agent.report import RCAReport
from e2ekg.agent.specialists import build_specialists, prompt
from e2ekg.agent.tools import make_tools
from e2ekg.graph.store import Store, graph_uri
from e2ekg.llm import MODEL_ID

EVIDENCE_TOOL = "candidate_evidence"
COMMANDER_TOOLS = ("find_symptom", "changes_in_window", "causal_chains", EVIDENCE_TOOL,
                   "read_findings", "raw_evidence", "sparql_select")
RECURSION_LIMIT = 200
# Every AutoRCA arm runs under this one profile: exactly the subagents it is given (no
# auto-added general-purpose one) and no shell.
HARNESS = HarnessProfile(excluded_tools=frozenset({"execute"}),
                         general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False))


class FailedRCA(RCAReport):
    """An investigation that ended without a report: scored as inconclusive, with the reason."""
    failure: str


def harnessed(model: str | BaseChatModel) -> BaseChatModel:
    """The chat model, with HARNESS registered for it. deepagents keeps profiles in a
    process-global registry keyed provider:model and computes that key with private helpers,
    hence the pinned deepagents version."""
    chat = init_chat_model(model) if isinstance(model, str) else model
    register_harness_profile(f"{get_model_provider(chat)}:{get_model_identifier(chat)}", HARNESS)
    return chat


def _private_copy(store: Store) -> Store:
    findings = str(graph_uri("findings", ""))
    work = Store(None)
    work.oxigraph.extend(q for q in store.oxigraph
                         if not str(getattr(q.graph_name, "value", "")).startswith(findings))
    return work


def commander_prompt(with_evidence_tool: bool = True) -> str:
    return prompt("autorca") + (f"\n\n{prompt(EVIDENCE_TOOL)}" if with_evidence_tool else "")


# ToolStrategy, not the provider's response_format: with a JSON schema bound, GLM-5.3-flash
# wrote a report at once, with no tool call (live run 1).
def build_autorca(store: Store, proc_root: Path, scenario: str,
                  model: str | BaseChatModel = f"fireworks:{MODEL_ID}", *, llm=None,
                  as_of: datetime | None = None, evidence_root: Path | None = None,
                  gate: bool = True, with_evidence_tool: bool = True):
    """The compiled agent over a private copy of `store` (the as-of store). `llm` lets the
    tools read nicknames; `as_of` is the store's cutoff; `/evidence/` maps to `evidence_root`
    (default: a new temporary directory); `gate` is the correlation engine's gate."""
    work = _private_copy(store)
    chat = harnessed(model)
    tools = make_tools(work, proc_root, scenario, "autorca", llm=llm, as_of=as_of, gate=gate)
    names = [n for n in COMMANDER_TOOLS if with_evidence_tool or n != EVIDENCE_TOOL]
    evidence = Path(evidence_root) if evidence_root else Path(tempfile.mkdtemp(prefix="autorca-"))
    evidence.mkdir(parents=True, exist_ok=True)
    return create_deep_agent(
        model=chat, tools=[tools[n] for n in names],
        system_prompt=commander_prompt(with_evidence_tool),
        subagents=build_specialists(work, proc_root, scenario, llm=llm, as_of=as_of),
        response_format=ToolStrategy(RCAReport), name="autorca",
        backend=CompositeBackend(default=StateBackend(), routes={
            "/evidence/": FilesystemBackend(root_dir=evidence, virtual_mode=True)}))


def run_rca(alert_id: str, scenario: str, *, root: Path = Path("data"),
            model: str | BaseChatModel = f"fireworks:{MODEL_ID}", llm=None,
            evidence_root: Path | None = None, callbacks: list | None = None,
            gate: bool = True, with_evidence_tool: bool = True) -> RCAReport:
    """Investigate `alert_id` over <root>/graph/<scenario>/as_of. `llm` defaults to the
    cached Fireworks client (the Resolver's nickname reader). A run that hits the recursion
    limit or ends without a report returns a FailedRCA."""
    graph = Path(root) / "graph" / scenario / "as_of"
    as_of = datetime.fromisoformat(json.loads((graph / "harvest.json").read_text())["as_of"])
    if llm is None:
        from e2ekg.llm import FireworksClient
        llm = FireworksClient()
    with tempfile.TemporaryDirectory(prefix="autorca-") as scratch:
        agent = build_autorca(Store(graph / "store", read_only=True),
                              Path(root) / "processed" / scenario, scenario, model, llm=llm,
                              as_of=as_of, evidence_root=evidence_root or Path(scratch),
                              gate=gate, with_evidence_tool=with_evidence_tool)
        try:
            out = agent.invoke(
                {"messages": [HumanMessage(f"Alert {alert_id} fired. Find its root cause and "
                                           "return the RCAReport.")]},
                config={"recursion_limit": RECURSION_LIMIT, "callbacks": callbacks or []})
        except GraphRecursionError:
            return _failed(f"GraphRecursionError: no report within {RECURSION_LIMIT} steps")
    report = out.get("structured_response")
    return report if report is not None else _failed(
        "no structured response: the agent stopped without returning an RCAReport")


def _failed(why: str) -> FailedRCA:
    return FailedRCA(verdict="inconclusive", hypotheses=[], summary=f"run failed: {why}",
                     failure=why)
