"""AutoRCA's fast path (PROPOSAL A.2): the Deep Path's three phases — specialists, candidate
weighing, report — collapsed into one-shot structured calls over a prefetched tool slice.

The insight: by the time a specialist's tool loop ends, everything it learned was computable
deterministically in advance. The LLM's irreplaceable job is judgment, not fetching. So:

    t=0.0s  specialist_slice: every specialist's full tool slice, computed with the SAME
            tools it would have called, plus the commander's candidate facts (<0.5 s)
    t=0.5s  7 specialists in parallel (asyncio.gather): each is ONE structured LLM call —
            its own prompt, its slice in the message, FindingIns back; the orchestrator
            records the findings in the graph exactly as record_finding would (same SHACL
            shapes, same URIs), so the case view and the trail are unchanged
    t=4.5s  synthesis: ONE structured LLM call over the facts (prompts/autorca_fast.md)
            -> RCAReport
    t=~9s   done: 8-9 LLM calls (the Deep Path: ~43)

Escalation, not regression: when the synthesis is not confident — the verdict is not
root_cause_found, the top confidence is low, or the top two hypotheses tie — the run hands
the alert to the Deep Path (run_rca over a fresh store copy), so the fast path never bounds
investigation quality, only the happy path. The Deep Path keeps the same invariants (R34,
R50, R53) and is what the default eval arms measure; the fast path is the opt-in `graph_fast`
arm (evals/arms.py).

    report = run_rca_fast("radar-09000", "hero")     # reads data/graph/hero/as_of only
"""
import asyncio
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel

from e2ekg.agent.autorca import PREFETCH_MINUTES, _private_copy, failed, harnessed, run_rca
from e2ekg.agent.report import FindingIn, RCAReport
from e2ekg.agent.specialists import SPECIALISTS, prompt
from e2ekg.agent.tools import make_tools
from e2ekg.agent.trail import OUTPUT_CHARS, Trail, findings_of
from e2ekg.graph.store import Store
from e2ekg.llm import MODEL_ID

FAST_MIN_CONFIDENCE = 0.6
"""A synthesis below this hands the alert to the Deep Path."""
FAST_TIE = 0.05
"""Top-two hypotheses within this confidence (different roots) are a conflict: escalate."""
CANDIDATE_CAP = 24
"""The most changes candidate_evidence examines for the synthesis (the window's, earliest kept)."""
CANDIDATE_CHUNK = 6
"""candidate_evidence is called in chunks of this many candidates, as the Deep Path's
commander does, so one chunk's answer never hits the tool's output cap."""


class FindingIns(BaseModel):
    """A fast-path specialist's structured answer: the findings its slice supports, in the
    shape record_finding validates."""
    findings: list[FindingIn] = []


def brief(alert_id: str, sym: dict, start: str) -> str:
    """The one-paragraph brief every fast-path call opens with: what the commander's step 1
    would have told the specialists."""
    inc = sym.get("incident")
    return (f"Alert {alert_id} fired on application {sym['alert']['app']} at {sym['alert']['at']}. "
            f"Symptom URI: {sym['symptom_uri']}. "
            + (f"Incident {inc['id']} (severity {inc.get('severity')}) was filed against "
               f"{', '.join(inc.get('affects') or []) or 'nothing'}. " if inc else "No incident was open. ")
            + f"The window examined runs from {start} to the cutoff {sym['as_of']}, skew-corrected UTC.")


def specialist_slice(store: Store, proc_root: Path, scenario: str, alert_id: str, *,
                     llm=None, as_of=None, gate=True) -> dict | None:
    """Everything the fast path needs, computed with the same tools the Deep Path would have
    called — no LLM call. Returns {"sym", "start", "slices", "chains", "changes", "facts"}:
    find_symptom's parsed answer, one {tool: answer} map per specialist, the engine's chains,
    the window's changes, and the candidate_evidence facts about every change in the window.
    None when find_symptom fails: without the brief there is nothing to judge, and the caller
    escalates (the Deep Path's commander can still work from the error)."""
    tools = make_tools(store, proc_root, scenario, "autorca", llm=llm, as_of=as_of, gate=gate)
    out = tools["find_symptom"].invoke({"alert_id": alert_id})
    try:
        sym = json.loads(out)
    except ValueError:
        return None
    start = (datetime.fromisoformat(sym["alert"]["at"].replace("Z", "+00:00"))
             - timedelta(minutes=PREFETCH_MINUTES)).isoformat()
    window = {"apps": [], "start": start, "end": sym["as_of"]}
    app = sym["alert"]["app"]
    chains = tools["causal_chains"].invoke({"symptom_uri": sym["symptom_uri"]})
    changes = tools["changes_in_window"].invoke(window)
    slices = {
        "topology": {"dependency_closure (direction down)": tools["dependency_closure"].invoke(
                         {"app": app, "direction": "down"}),
                     "dependency_closure (direction up)": tools["dependency_closure"].invoke(
                         {"app": app, "direction": "up"}),
                     "trace_edges": tools["trace_edges"].invoke(window)},
        "change": {"changes_in_window": changes},
        "metrics": {"anomalies_in_window": tools["anomalies_in_window"].invoke(window)},
        "logs": {"log_spikes": tools["log_spikes"].invoke(window)},
        "traces": {"trace_edges": tools["trace_edges"].invoke(window)},
        "alerts_incidents": {"find_symptom": out,
                             "alerts_incidents": tools["alerts_incidents"].invoke(
                                 {"start": start, "end": sym["as_of"]})},
        "history": {**{"similar_incidents": tools["similar_incidents"].invoke({"app": app, "kinds": []})},
                    **({"chat_claims": tools["chat_claims"].invoke(
                            {"channel": sym["incident"]["id"], "kinds": []})}
                       if sym.get("incident") else {})},
    }
    rows = json.loads(changes)
    uris = [r["uri"] for r in rows if r.get("uri")][:CANDIDATE_CAP]
    facts = None
    for i in range(0, len(uris), CANDIDATE_CHUNK):
        out = json.loads(tools["candidate_evidence"].invoke(
            {"alert_id": alert_id, "candidate_uris": uris[i:i + CANDIDATE_CHUNK]}))
        if facts is None:
            facts = out
        else:
            facts["candidates"] += out.get("candidates", [])
    return {"sym": sym, "start": start, "slices": slices, "chains": chains,
            "changes": changes, "facts": facts}


def specialist_message(name: str, brief_text: str, answers: dict[str, str]) -> str:
    """The fast-path specialist's one message: its own prompt (the method names the calls),
    then the slice's answers verbatim, then the reporting contract."""
    lines = [brief_text, "",
             "This run is one-shot: every tool call your method describes was made for you "
             "before you started, and each answer is below, verbatim as the tool returned it. "
             "Work only from these answers; do not invent anything they do not say.", ""]
    lines += [f"- {tool}: {answer}" for tool, answer in answers.items()]
    lines += ["", "Judge the answers and report the findings your method describes by "
              "returning them as the FindingIns tool call: {\"findings\": [{\"about_app\", "
              "\"claim\", \"signal_uris\", \"confidence\"}]}. signal_uris must be URIs that "
              "appear in the answers above — a finding whose evidence is not in the graph is "
              "rejected. At most two findings — the two your method weighs most — each "
              "claim a single sentence, or an empty findings list when the slice shows "
              "nothing worth reporting. The findings are recorded in the graph for you, "
              "validated exactly as record_finding validates them."]
    return "\n".join(lines)


async def _structured(chat, system: str, user: str, schema: type[BaseModel], callbacks):
    """One structured LLM call, the Deep Path's proven way: the schema bound as a tool, the
    model must call it, the tool call's args are the answer. A model that answers in content
    instead is parsed tolerantly."""
    bound = chat.bind_tools([convert_to_openai_tool(schema)], tool_choice="any")
    config = {"callbacks": callbacks} if callbacks else None
    out = await bound.ainvoke([SystemMessage(system), HumanMessage(user)], config=config)
    if out.tool_calls:
        return schema.model_validate(out.tool_calls[0]["args"])
    text = out.content if isinstance(out.content, str) else json.dumps(out.content)
    text = text[text.index("{"):text.rindex("}") + 1]     # prose fences off
    return schema.model_validate_json(text)


def _note(trail: Trail | None, tool: str, args: dict, output) -> None:
    if trail is not None:
        trail.note(tool, args, output)


async def _specialists(chat, batch: dict, scenario: str, callbacks, trail: Trail | None) -> None:
    """The seven one-shot specialist calls, gathered; each finding lands in the graph through
    the specialist's own record_finding tool (SHACL, URIs, cites exactly as the Deep Path)."""
    brief_text = brief(scenario, batch["sym"], batch["start"])

    async def one(name: str, answers: dict[str, str]):
        user = specialist_message(name, brief_text, answers)
        ins = await _structured(chat, prompt(name), user, FindingIns, callbacks)
        tools = make_tools(batch["work"], batch["proc"], scenario, name, llm=batch["llm"],
                           as_of=batch["as_of"], gate=batch["gate"])
        for f in ins.findings:
            out = tools["record_finding"].invoke({"f": f.model_dump()})
            _note(trail, "record_finding", {"specialist": name, "about_app": f.about_app,
                                            "claim": f.claim}, out)
        _note(trail, f"fast:{name}", {"tools": list(answers)},
              json.dumps([f.model_dump() for f in ins.findings])[:OUTPUT_CHARS])

    await asyncio.gather(*(one(name, answers) for name, answers in batch["slices"].items()))


def _digest(recorded: list[dict]) -> str:
    """The specialists' findings, as the synthesis reads them (read_findings' view)."""
    return "\n".join(f"- {f['specialist']} on {f['app']} (confidence {f['confidence']}): "
                     f"{f['claim']} cites {', '.join(f['cites'])}" for f in recorded) or \
        "(no specialist recorded a finding)"


async def _synthesis(chat, alert_id: str, batch: dict, findings: str, callbacks) -> RCAReport:
    user = "\n\n".join([
        brief(alert_id, batch["sym"], batch["start"]),
        f"The correlation engine's chains for the symptom:\n{batch['chains']}",
        f"Every change in the window:\n{batch['changes']}",
        "The facts about each candidate change (candidate_evidence):\n"
        + (json.dumps(batch["facts"], indent=1, default=str)
           if batch["facts"] else "(the window holds no change to examine)"),
        f"The specialists' findings as recorded:\n{findings}",
        "Weigh the explanations and return the RCAReport.",
    ])
    return await _structured(chat, prompt("autorca_fast"), user, RCAReport, callbacks)


def _weak(report: RCAReport) -> str | None:
    """Why the fast path should not trust this report, or None when it stands."""
    if report.verdict != "root_cause_found" or not report.hypotheses:
        return f"the synthesis did not find the root cause (verdict {report.verdict})"
    top = report.hypotheses[0]
    if top.confidence < FAST_MIN_CONFIDENCE:
        return f"the synthesis' confidence is {top.confidence:.2f} < {FAST_MIN_CONFIDENCE}"
    if len(report.hypotheses) > 1:
        second = report.hypotheses[1]
        if second.root_cause_uri != top.root_cause_uri and top.confidence - second.confidence <= FAST_TIE:
            return (f"the top two hypotheses tie within {FAST_TIE:.2f} "
                    f"({top.confidence:.2f} vs {second.confidence:.2f})")
    return None


def run_rca_fast(alert_id: str, scenario: str, *, root: Path = Path("data"),
                 model: str | BaseChatModel = f"fireworks:{MODEL_ID}", llm=None,
                 evidence_root: Path | None = None, callbacks: list | None = None,
                 gate: bool = True, trail: Trail | None = None,
                 escalate: bool = True) -> RCAReport:
    """The fast path over <root>/graph/<scenario>/as_of: prefetch, seven parallel one-shot
    specialists, one synthesis. Escalates to `run_rca` (the Deep Path, over a fresh store
    copy, so no fast-path finding reaches it) when the synthesis is weak or the prefetch
    failed. With escalate=False it returns the fast report as is (the evals' ablation).
    The fast path itself writes no evidence; `evidence_root` is where an escalation's Deep
    Path run keeps its /evidence/, as in run_rca."""
    graph = Path(root) / "graph" / scenario / "as_of"
    as_of = datetime.fromisoformat(json.loads((graph / "harvest.json").read_text())["as_of"])
    if llm is None:
        from e2ekg.llm import FireworksClient
        llm = FireworksClient()

    def deep(why: str) -> RCAReport:
        _note(trail, "escalate", {"reason": why}, "handing the alert to the Deep Path")
        return run_rca(alert_id, scenario, root=root, model=model, llm=llm,
                       evidence_root=evidence_root, callbacks=callbacks, gate=gate, trail=trail)

    with tempfile.TemporaryDirectory(prefix="autorca-fast-") as scratch:
        work = _private_copy(Store(graph / "store", read_only=True))
        proc = Path(root) / "processed" / scenario
        batch = specialist_slice(work, proc, scenario, alert_id, llm=llm, as_of=as_of, gate=gate)
        if batch is None:
            return deep("prefetch failed: find_symptom returned no symptom") if escalate else failed(
                "prefetch failed: find_symptom returned no symptom")
        chat = harnessed(model)   # not needed before this point: the prefetch is deterministic
        batch |= {"work": work, "proc": proc, "llm": llm, "as_of": as_of, "gate": gate}
        _note(trail, "fast:prefetch",
              {"per_specialist": {n: list(a) for n, a in batch["slices"].items()},
               "candidate_facts": batch["facts"] is not None},
              f"the Deep Path's first {8 + len(batch['slices'])} tool calls, made directly")

        async def fast() -> RCAReport:
            await _specialists(chat, batch, scenario, callbacks, trail)
            recorded = findings_of(work, scenario)
            _note(trail, "fast:findings", {"recorded": len(recorded)}, _digest(recorded))
            return await _synthesis(chat, alert_id, batch, _digest(recorded), callbacks)

        try:
            report = asyncio.run(fast())
        except Exception as e:     # a failed specialist call is an escalation, not a crash
            return deep(f"the fast path failed: {type(e).__name__}: {e}") if escalate else failed(
                f"the fast path failed: {type(e).__name__}: {e}")
        if trail is not None:
            trail.findings = findings_of(work, scenario)
        if escalate and (why := _weak(report)):
            return deep(why)
        return report
