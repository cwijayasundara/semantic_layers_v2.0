"""The AutoRCA agent: seven specialists with their own tools and prompts, a commander that
weighs facts and decides, and no way to reach the full store or the answer key.

Unit tests drive the real compiled agent with a scripted tool-calling model: one policy
plays the commander and every specialist. The scripted commander reasons the way the prompt
teaches, over the facts candidate_evidence returns for the candidates it names, because no
tool hands it a verdict to copy."""
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent_helpers import copy_store, fake_tool_model, subagent_names, tool_names
from deepagents import create_deep_agent
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from e2ekg.agent import autorca
from e2ekg.agent.autorca import FailedRCA, build_autorca, harnessed, run_rca
from e2ekg.agent.report import FindingIn, RCAReport
from e2ekg.agent.specialists import DESCRIPTIONS, PROMPTS, SPECIALISTS, build_specialists
from e2ekg.agent.tools import make_tools
from e2ekg.graph import store as store_mod
from e2ekg.graph.uris import change_uri
from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.variants import variant_scenarios

HERO = hero_scenario()
ALERT, ALERT_APP = "radar-09000", "homepage-graphql"
WINDOW = {"start": "2026-10-14T21:00Z", "end": "2026-10-14T23:00Z"}
GRAPH_TOOLS = {"find_symptom", "dependency_closure", "changes_in_window", "anomalies_in_window",
               "log_spikes", "trace_edges", "alerts_incidents", "similar_incidents",
               "chat_claims", "causal_chains", "candidate_evidence", "raw_evidence",
               "sparql_select", "record_finding", "read_findings"}
FIRST_CALL = {"topology": ("trace_edges", WINDOW | {"apps": []}),
              "change": ("changes_in_window", WINDOW | {"apps": []}),
              "metrics": ("anomalies_in_window", WINDOW | {"apps": []}),
              "logs": ("log_spikes", WINDOW | {"apps": []}),
              "traces": ("trace_edges", WINDOW | {"apps": []}),
              "alerts_incidents": ("find_symptom", {"alert_id": ALERT}),
              "history": ("chat_claims", {"channel": "INC-5377", "kinds": []})}


def _call(name: str, args: dict, i: int = 0) -> dict:
    return {"name": name, "args": args, "id": f"{name}-{i}"}


def _last(messages, name: str) -> str:
    return next(m.content for m in reversed(messages)
                if isinstance(m, ToolMessage) and m.name == name)


def _all(messages, name: str) -> list[str]:
    return [m.content for m in messages if isinstance(m, ToolMessage) and m.name == name]


def _specialist(bound: frozenset[str]) -> str:
    return next(n for n, t in SPECIALISTS.items() if bound & GRAPH_TOOLS == {*t, "record_finding"})


def investigator(offered: dict, transcript: list):
    """The scripted policy: the commander follows its prompt's six steps; each specialist
    makes one call, records a finding citing the first URI it got back, and reports."""
    def policy(messages, bound):
        transcript.extend(m.content for m in messages if isinstance(m, ToolMessage))
        n = sum(isinstance(m, AIMessage) for m in messages)
        if "task" in bound:
            offered["commander"] = bound
            return commander(messages, n)
        name = _specialist(bound)
        offered[name] = bound
        return specialist(name, messages, n)
    return policy


def commander(messages, n: int) -> AIMessage:
    if n == 0:
        return AIMessage("", tool_calls=[_call("find_symptom", {"alert_id": ALERT})])
    if n == 1:
        return AIMessage("", tool_calls=[
            _call("task", {"subagent_type": s, "description": f"Alert {ALERT} on {ALERT_APP} at "
                           f"2026-10-14T22:30Z; window {WINDOW['start']} to {WINDOW['end']}."}, i)
            for i, s in enumerate(SPECIALISTS)])
    if n == 2:
        symptom = json.loads(_last(messages, "find_symptom"))["symptom_uri"]
        return AIMessage("", tool_calls=[
            _call("read_findings", {}), _call("causal_chains", {"symptom_uri": symptom}),
            _call("changes_in_window", {"apps": [], "start": "2026-10-14T21:00Z",
                                        "end": "2026-10-14T22:30Z"})])
    if n == 3:
        roots = [c["links"][0][3] for c in json.loads(_last(messages, "causal_chains"))]
        cited = [u for f in json.loads(_last(messages, "read_findings")) for u in f["cites"]
                 if "/chg/" in u]
        seen = [c["uri"] for c in json.loads(_last(messages, "changes_in_window")) if "uri" in c]
        named = sorted({*roots, *cited, *seen})
        return AIMessage("", tool_calls=[
            _call("candidate_evidence", {"alert_id": ALERT, "candidate_uris": named[i:i + 6]}, i)
            for i in range(0, len(named), 6)])
    facts = [c for out in _all(messages, "candidate_evidence")
             for c in json.loads(out)["candidates"] if "error" not in c]
    judged = [(c, ruled_out(c)) for c in facts]
    alive = sorted((c for c, why in judged if why is None), key=lambda c: c["at"])
    refuted = [f"{c['uri']} refuted: {why}" for c, why in judged if why]
    report = {"verdict": "root_cause_found" if len(alive) == 1 else "inconclusive",
              "hypotheses": [{"root_cause_uri": c["uri"], "chain": [c["uri"]],
                              "confidence": 0.8, "evidence": [c["uri"]],
                              "refuted_alternatives": refuted} for c in alive],
              "summary": "scripted"}
    return AIMessage("", tool_calls=[_call("RCAReport", report)])


def ruled_out(c: dict) -> str | None:
    """The scripted commander's reading of one candidate's facts: the step 6 method."""
    if c["call_path"] is None:
        return f"{ALERT_APP} does not depend on {c['app']}"
    if c["minutes_before_onset"] is None or c["minutes_before_onset"] < 0:
        return "landed after the degradation began"
    if any(h["status"] == "not_degraded" for h in c["call_path"]):
        return "a call on its path stayed healthy"
    if c["rooted_before"]["n"]:
        return f"landed into degradation {c['rooted_before']['first'][0]['rooted_in'][0]} explains"
    earlier = [x for x in c["same_app_changes"]["first"] if x["at"] < c["at"]]
    if c["kind"] == "scaling" and earlier:
        return f"a reaction to {earlier[0]['uri']}"
    return None


def specialist(name: str, messages, n: int) -> AIMessage:
    tool, args = FIRST_CALL[name]
    if n == 0:
        return AIMessage("", tool_calls=[_call(tool, args)])
    if n == 1:
        out = json.loads(_last(messages, tool))
        rows = [out["alert"]] if isinstance(out, dict) else out
        row = next((r for r in rows if r.get("uri")), None)
        if row is None:
            return AIMessage("no finding")
        return AIMessage("", tool_calls=[_call("record_finding", {"f": {
            "about_app": row.get("app") or row.get("callee") or ALERT_APP,
            "claim": f"{name} saw {row['uri']}", "signal_uris": [row["uri"]],
            "confidence": 0.5}})])
    return AIMessage(f"finding {_last(messages, 'record_finding')}")


@pytest.fixture(scope="module")
def hero_root(tmp_path_factory, harvested_hero, harvested_hero_as_of, preprocessed_hero):
    """A data root laid out like data/: the as-of store, and beside it the full store and the
    answer key the agent must never reach."""
    root = tmp_path_factory.mktemp("data")
    for link, target in {"raw/hero": preprocessed_hero.raw,
                         "raw/_answers": preprocessed_hero.answers,
                         "processed/hero": preprocessed_hero.proc,
                         "graph/hero/store": harvested_hero.out / "store",
                         "graph/hero/as_of": harvested_hero_as_of.out}.items():
        (root / link).parent.mkdir(parents=True, exist_ok=True)
        (root / link).symlink_to(target)
    return root


@pytest.fixture(scope="module")
def uris(harvested_hero_as_of) -> dict[str, str]:
    answers = json.loads((harvested_hero_as_of.answers / "hero.json").read_text())
    ext = {v: k for k, v in answers["external_ids"].items()}
    return {c.id: str(change_uri("hero", c.kind, ext[c.id])) for c in HERO.changes if c.id in ext}


@pytest.fixture(scope="module")
def scripted_run(hero_root, scripted_llm, harvested_hero_as_of):
    """One run_rca over `hero_root` with the scripted model, recording every store opened and
    every file Python opened while it ran."""
    offered, transcript, stores, files = {}, [], [], []
    real_init = store_mod.Store.__init__

    def recording_init(self, path, read_only=False):
        stores.append((Path(path) if path else None, read_only))
        real_init(self, path, read_only)

    def audit(event, args):
        if event == "open" and recording:
            files.append(str(args[0]))

    before = harvested_hero_as_of.store.quad_count()
    mp = pytest.MonkeyPatch()
    mp.setattr(store_mod.Store, "__init__", recording_init)
    recording = [True]
    sys.addaudithook(audit)
    try:
        report = run_rca(ALERT, "hero", root=hero_root, llm=scripted_llm,
                         model=fake_tool_model(investigator(offered, transcript)),
                         evidence_root=hero_root / "agent" / "hero")
    finally:
        recording.clear()
        mp.undo()
    return {"report": report, "offered": offered, "transcript": transcript, "stores": stores,
            "files": files, "quads_before": before}


def test_agent_builds_with_seven_specialists(harvested_hero_as_of, scripted_llm):
    h = harvested_hero_as_of
    agent = build_autorca(h.store, h.proc, "hero", model=fake_tool_model(), llm=scripted_llm,
                          evidence_root=h.out.parent / "agent")
    assert set(subagent_names(agent)) == set(SPECIALISTS) and len(SPECIALISTS) == 7


@pytest.mark.parametrize("name", SPECIALISTS)
def test_specialist_tool_isolation(harvested_hero_as_of, scripted_llm, name):
    h = harvested_hero_as_of
    subs = {s["name"]: s for s in build_specialists(h.store, h.proc, "hero", llm=scripted_llm)}
    assert tool_names(subs[name]) == {*SPECIALISTS[name], "record_finding"}
    assert tool_names(subs["logs"]) == {"log_spikes", "raw_evidence", "record_finding"}


def test_each_agent_is_offered_only_its_own_graph_tools(scripted_run):
    offered = scripted_run["offered"]
    assert set(offered) == {"commander", *SPECIALISTS}
    for name, tools in SPECIALISTS.items():
        assert offered[name] & GRAPH_TOOLS == {*tools, "record_finding"}, name
    assert offered["commander"] & GRAPH_TOOLS == set(autorca.COMMANDER_TOOLS)
    assert "execute" not in set().union(*offered.values())


def test_the_scripted_investigation_names_the_root_and_refutes_every_herring(scripted_run, uris):
    r = scripted_run["report"]
    assert isinstance(r, RCAReport) and r.verdict == "root_cause_found"
    top = r.hypotheses[0]
    assert top.root_cause_uri == uris["chg-cfg-0001"]
    refuted = {a.split(" ", 1)[0] for a in top.refuted_alternatives}
    assert {uris[h] for h in HERO.answer_key.red_herring_change_ids} <= refuted


def test_every_specialist_with_a_signal_recorded_a_finding(scripted_run):
    findings = json.loads(next(t for t in scripted_run["transcript"]
                               if t.startswith('[{"uri":"https://kg.local/ops/finding/')))
    assert {f["specialist"] for f in findings} == set(SPECIALISTS)


def test_the_findings_never_reach_the_store_on_disk(scripted_run, harvested_hero_as_of):
    assert harvested_hero_as_of.store.quad_count() == scripted_run["quads_before"]


def test_run_rca_opens_only_the_as_of_store(scripted_run, hero_root):
    assert scripted_run["stores"][0] == (hero_root / "graph/hero/as_of/store", True)
    assert all(p is None for p, _ in scripted_run["stores"][1:])


def test_run_rca_never_opens_the_answer_key_or_the_full_store(scripted_run, hero_root):
    assert scripted_run["files"], "the audit hook saw no file at all"
    banned = [str(hero_root / "raw/_answers"), str(hero_root / "graph/hero/store"),
              str((hero_root / "raw/_answers").resolve()),
              str((hero_root / "graph/hero/store").resolve())]
    assert not [f for f in scripted_run["files"] if any(f.startswith(b) for b in banned)]


def test_nothing_the_agent_read_is_from_after_the_cutoff(scripted_run):
    stamps = {s for t in scripted_run["transcript"]
              for s in re.findall(r"2026-10-1\dT\d\d:\d\d(?::\d\d(?:\.\d+)?)?Z", t)}
    assert stamps and all(s[:16] <= "2026-10-14T23:00" for s in stamps)


def prober(transcript: list, calls: list[dict]):
    def policy(messages, bound):
        transcript.extend(m.content for m in messages if isinstance(m, ToolMessage))
        if not any(isinstance(m, AIMessage) for m in messages):
            return AIMessage("", tool_calls=[_call(c["name"], c["args"], i)
                                             for i, c in enumerate(calls)])
        return AIMessage("", tool_calls=[_call("RCAReport", {
            "verdict": "inconclusive", "hypotheses": [], "summary": "probe"})])
    return policy


def _probes(answers: Path) -> list[dict]:
    blob = (answers / "hero.json").read_bytes()[:40]
    forged = f"events|../_answers/hero.json|0|40|{hashlib.sha256(blob).hexdigest()[:16]}"
    return [{"name": "sparql_select", "args": {"query": "SELECT ?i ?t WHERE { ?i inc:correctedAt ?t }"}},
            {"name": "sparql_select", "args": {"query": 'SELECT ?s WHERE { ?s ?p ?o . '
                                                        'FILTER(CONTAINS(STR(?o), "chg-cfg-0001")) }'}},
            {"name": "raw_evidence", "args": {"uri_or_selector": forged}},
            {"name": "glob", "args": {"pattern": "**/*", "path": "/"}},
            {"name": "ls", "args": {"path": "/evidence/"}},
            {"name": "read_file", "args": {"file_path": "/evidence/../../raw/_answers/hero.json"}}]


def _probe(store, h, scripted_llm, evidence: Path) -> list[str]:
    transcript = []
    agent = build_autorca(store, h.proc, "hero", llm=scripted_llm, evidence_root=evidence,
                          model=fake_tool_model(prober(transcript, _probes(h.answers))))
    agent.invoke({"messages": [HumanMessage("probe")]})
    return transcript


def test_the_agents_own_tools_cannot_reach_the_answer_key_or_the_full_store(
        harvested_hero_as_of, scripted_llm, tmp_path):
    h = harvested_hero_as_of
    correction, root_id, forged, glob, ls, read = _probe(h.store, h, scripted_llm, tmp_path)
    assert json.loads(correction) == [] and json.loads(root_id) == []
    assert json.loads(forged) == []
    for listing in (glob, ls):
        assert "_answers" not in listing and "hero.json" not in listing
    assert "red_herring" not in read and "chg-cfg-0001" not in read


def test_the_same_probe_over_the_full_store_does_see_the_correction(
        harvested_hero, harvested_hero_as_of, scripted_llm, tmp_path):
    h = harvested_hero_as_of
    correction = _probe(harvested_hero.store, h, scripted_llm, tmp_path)[0]
    assert json.loads(correction)


def _answer_key_terms() -> set[str]:
    """Every app on any scenario's causal chain, every root and herring change, their apps
    and distinctive aliases, and the words that would give the game away."""
    generic = {"search", "profiles", "entitlements"}
    terms = {"TMS", "TBL", "herring", "red herring", "root_cause_change_id", "_answers"}
    for s in (HERO, *variant_scenarios()):
        ak, by_id = s.answer_key, {c.id: c for c in s.changes}
        apps = {link.app for link in ak.causal_chain}
        for cid in (ak.root_cause_change_id, *ak.red_herring_change_ids):
            if cid:
                terms.add(cid)
                apps.add(by_id[cid].app)
                terms |= {v for v in by_id[cid].attrs.values() if len(v) > 3}
        catalog = {a.id: a for a in s.catalog}
        terms |= apps | {al for a in apps for al in catalog[a].aliases} - generic
    return terms


def _model_facing_text(h, scripted_llm) -> dict[str, str]:
    """Everything the models read before any tool runs: every prompt, every tool's schema
    (its description and its arguments' descriptions), every specialist's description."""
    prompts = {p.name: p.read_text() for p in PROMPTS.glob("*.md")}
    assert set(prompts) == {f"{n}.md" for n in [*SPECIALISTS, "autorca", "candidate_evidence", "raw_only"]}
    tools = make_tools(copy_store(h.store), h.proc, "hero", "leak", llm=scripted_llm)
    return {**prompts,
            **{f"tool:{n}": json.dumps(convert_to_openai_tool(t)) for n, t in tools.items()},
            **{f"description:{n}": d for n, d in DESCRIPTIONS.items()}}


def test_nothing_the_models_read_names_an_app_change_or_answer_from_any_scenario(
        harvested_hero_as_of, scripted_llm):
    answers = json.loads((harvested_hero_as_of.answers / "hero.json").read_text())
    terms = _answer_key_terms() | set(answers["external_ids"])
    texts = _model_facing_text(harvested_hero_as_of, scripted_llm)
    hits = {(name, t) for name, text in texts.items() for t in terms
            if re.search(rf"(?<![\w-]){re.escape(t)}(?![\w-])", text, re.I)}
    assert not hits


def test_the_leak_grep_reads_tool_argument_descriptions(harvested_hero_as_of, scripted_llm):
    texts = _model_facing_text(harvested_hero_as_of, scripted_llm)
    assert "Application id or a name people use for it" in texts["tool:dependency_closure"]


def test_the_leak_grep_would_catch_an_app_name():
    terms = _answer_key_terms()
    assert {"title-metadata-svc", "api-gateway", "chg-cfg-0001", "TMS"} <= terms


@pytest.mark.parametrize("name", SPECIALISTS)
def test_every_specialist_prompt_ends_with_the_reporting_rule(name):
    assert (PROMPTS / f"{name}.md").read_text().rstrip().endswith(
        "Report only through record_finding. Return one line naming the finding URIs.")


VERDICT_KEYS = {"survives", "refuted_by", "supported_by", "verdict", "refuted", "passes",
                "ok", "rank", "score", "checks", "conclusion"}


def _keys(obj) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | set().union(*(_keys(v) for v in obj.values()))
    if isinstance(obj, list):
        return set().union(*(_keys(v) for v in obj))
    return set()


def test_no_tool_the_agents_called_returned_a_verdict_or_a_ranking(scripted_run):
    outputs = []
    for t in scripted_run["transcript"]:
        try:
            outputs.append(json.loads(t))
        except ValueError:
            continue
    assert any("candidates" in o for o in outputs if isinstance(o, dict))
    assert not set().union(*map(_keys, outputs)) & VERDICT_KEYS


def test_a_relay_commander_has_no_candidate_list_to_copy(harvested_hero_as_of, scripted_llm,
                                                         tmp_path):
    h = harvested_hero_as_of
    transcript = []
    relay = prober(transcript, [{"name": "candidate_evidence",
                                 "args": {"alert_id": ALERT, "candidate_uris": []}}])
    agent = build_autorca(h.store, h.proc, "hero", llm=scripted_llm, evidence_root=tmp_path,
                          model=fake_tool_model(relay))
    agent.invoke({"messages": [HumanMessage("relay")]})
    assert transcript[0].startswith("error")


def _offered(with_evidence_tool: bool, h, scripted_llm, tmp_path):
    seen = {}

    def policy(messages, bound):
        if "task" in bound:
            seen["tools"] = bound
            seen["system"] = next(m.content for m in messages if isinstance(m, SystemMessage))
        return AIMessage("", tool_calls=[_call("RCAReport", {
            "verdict": "inconclusive", "hypotheses": [], "summary": "probe"})])
    agent = build_autorca(h.store, h.proc, "hero", llm=scripted_llm, evidence_root=tmp_path,
                          model=fake_tool_model(policy), with_evidence_tool=with_evidence_tool)
    agent.invoke({"messages": [HumanMessage("probe")]})
    return seen


def test_the_evidence_tool_and_its_prompt_come_and_go_together(harvested_hero_as_of,
                                                               scripted_llm, tmp_path):
    h = harvested_hero_as_of
    on, off = (_offered(w, h, scripted_llm, tmp_path) for w in (True, False))
    assert on["tools"] & GRAPH_TOOLS == set(autorca.COMMANDER_TOOLS)
    assert off["tools"] & GRAPH_TOOLS == set(autorca.COMMANDER_TOOLS) - {"candidate_evidence"}
    assert "candidate_evidence" in on["system"] and "candidate_evidence" not in off["system"]


def test_an_agent_built_without_specialists_gets_no_extra_subagent_and_no_shell():
    offered = {}

    def policy(messages, bound):
        offered["tools"] = bound
        return AIMessage("done")
    agent = create_deep_agent(model=harnessed(fake_tool_model(policy)), tools=[],
                              system_prompt="raw only")
    agent.invoke({"messages": [HumanMessage("go")]})
    assert "execute" not in offered["tools"] and "task" not in offered["tools"]
    assert {"ls", "read_file"} <= offered["tools"]


def _writer(listing: list):
    def policy(messages, bound):
        n = sum(isinstance(m, AIMessage) for m in messages)
        if n == 0:
            return AIMessage("", tool_calls=[_call("ls", {"path": "/evidence/"})])
        if n == 1:
            listing.append(_last(messages, "ls"))
            return AIMessage("", tool_calls=[_call("write_file", {
                "file_path": "/evidence/note.md", "content": "suspect everything"})])
        return AIMessage("", tool_calls=[_call("RCAReport", {
            "verdict": "inconclusive", "hypotheses": [], "summary": "noted"})])
    return policy


def test_each_run_gets_a_fresh_evidence_directory_unless_one_is_named(hero_root, scripted_llm,
                                                                      tmp_path):
    fresh, kept = [], []
    for _ in range(2):
        run_rca(ALERT, "hero", root=hero_root, llm=scripted_llm,
                model=fake_tool_model(_writer(fresh)))
    assert not any("note.md" in x for x in fresh)
    for _ in range(2):
        run_rca(ALERT, "hero", root=hero_root, llm=scripted_llm, evidence_root=tmp_path,
                model=fake_tool_model(_writer(kept)))
    assert "note.md" not in kept[0] and "note.md" in kept[1]
    assert (tmp_path / "note.md").read_text() == "suspect everything"


def test_a_run_that_hits_the_recursion_limit_returns_a_failed_report(hero_root, scripted_llm,
                                                                     monkeypatch):
    monkeypatch.setattr(autorca, "RECURSION_LIMIT", 6)
    forever = fake_tool_model(lambda messages, bound: AIMessage(
        "", tool_calls=[_call("find_symptom", {"alert_id": ALERT},
                              sum(isinstance(m, AIMessage) for m in messages))]))
    r = run_rca(ALERT, "hero", root=hero_root, llm=scripted_llm, model=forever)
    assert isinstance(r, FailedRCA) and r.verdict == "inconclusive" and not r.hypotheses
    assert "GraphRecursionError" in r.failure


def test_a_run_that_ends_without_a_report_returns_a_failed_report(hero_root, scripted_llm):
    r = run_rca(ALERT, "hero", root=hero_root, llm=scripted_llm,
                model=fake_tool_model(lambda messages, bound: AIMessage("I give up")))
    assert isinstance(r, FailedRCA) and r.verdict == "inconclusive"
    assert "structured response" in r.failure


def test_findings_from_an_earlier_run_never_reach_a_new_agent(harvested_hero_as_of,
                                                              scripted_llm, tmp_path):
    h = harvested_hero_as_of
    store = copy_store(h.store)
    alert = f"https://kg.local/alrt/alert/hero/{ALERT}"
    old = make_tools(store, h.proc, "hero", "earlier", llm=scripted_llm)["record_finding"]
    assert "uri" in json.loads(old.invoke({"f": FindingIn(
        about_app=ALERT_APP, claim="earlier run", signal_uris=[alert], confidence=0.5)}))
    transcript = []
    agent = build_autorca(store, h.proc, "hero", llm=scripted_llm, evidence_root=tmp_path,
                          model=fake_tool_model(prober(transcript, [
                              {"name": "read_findings", "args": {}},
                              {"name": "find_symptom", "args": {"alert_id": ALERT}}])))
    agent.invoke({"messages": [HumanMessage("probe")]})
    assert json.loads(transcript[0]) == [] and json.loads(transcript[1])["alert"]["uri"] == alert


class LiveLog(UsageMetadataCallbackHandler):
    """Token usage (the parent's), plus model calls and tool calls by name."""
    def __init__(self):
        super().__init__()
        self.model_calls, self.tools, self.tool_errors = 0, Counter(), Counter()

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.model_calls += 1

    def on_tool_start(self, serialized, input_str, **kwargs):
        self.tools[(serialized or {}).get("name") or kwargs.get("name") or "?"] += 1

    def on_tool_end(self, output, **kwargs):
        text = getattr(output, "content", output)
        if isinstance(text, str) and text.startswith("error"):
            self.tool_errors[kwargs.get("name") or "?"] += 1


@pytest.mark.live
def test_live_hero_rca(tmp_path):
    if not os.environ.get("FIREWORKS_API_KEY"):
        pytest.skip("FIREWORKS_API_KEY is not set")
    log, t0, report, failure = LiveLog(), time.perf_counter(), None, None
    try:
        report = run_rca(ALERT, "hero", evidence_root=tmp_path, callbacks=[log])
    except Exception as e:
        failure = f"{type(e).__name__}: {e}"
        raise
    finally:
        out = Path(".cache") / f"live_hero_rca-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
        out.write_text(json.dumps({
            "wall_s": round(time.perf_counter() - t0, 1), "model_calls": log.model_calls,
            "usage": log.usage_metadata, "tools": log.tools, "tool_errors": log.tool_errors,
            "failure": failure, "report": report and report.model_dump()},
            indent=1, default=str))
    r = report
    answers = json.loads(Path("data/raw/_answers/hero.json").read_text())
    ext = {v: k for k, v in answers["external_ids"].items()}
    root = str(change_uri("hero", "config_change", ext["chg-cfg-0001"]))
    assert r.verdict == "root_cause_found"
    assert r.hypotheses[0].root_cause_uri == root
    assert _reaches(r.hypotheses[0].evidence, set(answers["evidence_raw_ids"]))


def _reaches(uris: list[str], evidence_ids: set[str]) -> set[str]:
    """The cited URIs whose raw records (their own, or those of what they derive from or
    cite) include an answer-key evidence record."""
    from e2ekg.graph.store import Store
    from e2ekg.preprocess.selector import selected_raw_ids
    store, proc = Store(Path("data/graph/hero/as_of/store"), read_only=True), Path("data/processed/hero")
    hits = set()
    for u in uris:
        if not u.startswith("https://kg.local/"):
            continue
        vals = [r["v"] for r in store.query(
            "PREFIX ops: <https://kg.local/ops#> PREFIX prov: <http://www.w3.org/ns/prov#> "
            f"SELECT DISTINCT ?v WHERE {{ <{u}> (prov:wasDerivedFrom|ops:cites)* ?x . "
            "?x ops:rawSelector ?v }")]
        ids = {v for v in vals if not v.startswith("{")}
        ids |= selected_raw_ids(proc, [v for v in vals if v.startswith("{")])
        if ids & evidence_ids:
            hits.add(u)
    return hits


def test_rca_cli_prints_the_report_and_exits_on_the_verdict(monkeypatch, capsys, tmp_path):
    from e2ekg.cli import main
    seen = {}

    def fake_run(alert, scenario, *, root, evidence_root):
        seen.update(alert=alert, scenario=scenario, root=root, evidence=evidence_root)
        return RCAReport(verdict="inconclusive", hypotheses=[], summary="nothing survived")
    monkeypatch.setattr(autorca, "run_rca", fake_run)
    code = main(["rca", "--scenario", "hero", "--alert", ALERT, "--root", str(tmp_path)])
    assert code == 1 and seen == {"alert": ALERT, "scenario": "hero", "root": tmp_path,
                                  "evidence": None}
    assert json.loads(capsys.readouterr().out)["summary"] == "nothing survived"


def test_live_models_carry_a_request_timeout(monkeypatch):
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-key-not-used")
    from e2ekg.agent.autorca import harnessed
    from e2ekg.llm import MODEL_ID, REQUEST_TIMEOUT_S
    chat = harnessed(f"fireworks:{MODEL_ID}")
    assert chat.request_timeout == REQUEST_TIMEOUT_S and chat.max_retries == 3
