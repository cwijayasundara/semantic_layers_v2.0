"""The eval runner: its plan, its resume, its arms and its scoring, on fixture stores and fake
models. No network."""
import pytest
from agent_helpers import fake_tool_model
from langchain_core.messages import AIMessage, ToolMessage

from e2ekg.agent.report import RCAReport
from e2ekg.evals import run as ev
from e2ekg.evals.run import (
    ARMS, RunSpec, execute, make_plan, resolve_arms, resolve_scenarios, run_evals, run_one,
    score_runs,
)

ALERT = "radar-09000"


# --- plan and resume ---------------------------------------------------------------------

def test_the_plan_runs_the_deterministic_arms_once_and_the_llm_arms_round_by_round():
    plan = make_plan(["hero", "v-control"], 2, ["graph", "engine_top", "raw_only"])
    assert [p.name for p in plan] == [
        "hero__engine_top__0", "v-control__engine_top__0",
        "hero__graph__0", "hero__raw_only__0", "v-control__graph__0", "v-control__raw_only__0",
        "hero__graph__1", "hero__raw_only__1", "v-control__graph__1", "v-control__raw_only__1"]


def fake_do(calls: list):
    def do(spec: RunSpec) -> dict:
        calls.append(spec.name)
        return {"report": RCAReport(verdict="inconclusive", hypotheses=[], summary="s").model_dump(),
                "seconds": 1.0}
    return do


def test_a_second_invocation_skips_what_is_done_and_runs_the_rest(tmp_path):
    plan, calls = make_plan(["hero"], 3, ["graph"]), []
    first = execute(plan, tmp_path, fake_do(calls), max_runs=2, say=lambda _: None)
    assert first.done == ["hero__graph__0", "hero__graph__1"] == calls
    assert first.deferred == ["hero__graph__2"]
    second = execute(plan, tmp_path, fake_do(calls), say=lambda _: None)
    assert second.skipped == ["hero__graph__0", "hero__graph__1"]
    assert second.done == ["hero__graph__2"] and len(calls) == 3
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == [
        f"hero__graph__{i}.json" for i in range(3)]


def test_max_runs_counts_llm_runs_and_never_holds_back_a_deterministic_arm(tmp_path):
    plan, calls = make_plan(["hero", "v-control"], 1, ["engine_top", "graph"]), []
    ex = execute(plan, tmp_path, fake_do(calls), max_runs=1, say=lambda _: None)
    assert ex.done == ["hero__engine_top__0", "v-control__engine_top__0", "hero__graph__0"]
    assert ex.deferred == ["v-control__graph__0"]


def test_a_run_that_raises_saves_nothing_and_is_retried_next_time(tmp_path):
    plan, boom = make_plan(["hero"], 1, ["graph"]), [True]

    def flaky(spec):
        if boom.pop() if boom else False:
            raise ConnectionError("network down")
        return fake_do([])(spec)
    ex = execute(plan, tmp_path, flaky, say=lambda _: None)
    assert ex.errors == ["hero__graph__0"] and not list((tmp_path / "runs").glob("*.json"))
    assert "network down" in (tmp_path / "errors.jsonl").read_text()
    assert execute(plan, tmp_path, flaky, say=lambda _: None).done == ["hero__graph__0"]


def test_run_files_are_written_whole_or_not_at_all(tmp_path):
    execute(make_plan(["hero"], 1, ["graph"]), tmp_path, fake_do([]), say=lambda _: None)
    assert not list((tmp_path / "runs").glob("*.tmp"))


def test_unknown_arms_and_scenarios_are_refused_by_name(eval_root):
    assert resolve_arms("all") == list(ARMS) and resolve_arms("graph,graph") == ["graph"]
    with pytest.raises(ValueError, match="no_gate"):
        resolve_arms("graph,no_gate")
    assert resolve_scenarios(eval_root, "all") == ["hero", "v-control"]
    assert resolve_scenarios(eval_root, "v-control") == ["v-control"]
    with pytest.raises(ValueError, match="v-nope"):
        resolve_scenarios(eval_root, "hero,v-nope")


# --- the deterministic arms --------------------------------------------------------------

@pytest.fixture(scope="module")
def engine_runs(eval_root):
    return {(sc, arm): run_one(RunSpec(sc, arm, 0), root=eval_root, evals_dir=eval_root / "evals")
            for sc in ("hero", "v-control") for arm in ev.ENGINE_ARMS}


def test_engine_top_on_the_hero_reports_the_true_root_first(engine_runs, eval_root, eval_uri):
    run = engine_runs["hero", "engine_top"]
    assert run["alert"] == ALERT and run["seconds"] >= 0
    assert run["report"]["verdict"] == "root_cause_found"
    assert run["report"]["hypotheses"][0]["root_cause_uri"] == eval_uri("hero", "chg-cfg-0001")
    chain = run["report"]["hypotheses"][0]["chain"]
    assert chain[0] == eval_uri("hero", "chg-cfg-0001") and len(chain) == len(set(chain)) > 3


def test_engine_top_on_the_control_abstains(engine_runs):
    run = engine_runs["v-control", "engine_top"]
    assert run["report"]["verdict"] == "inconclusive" and run["report"]["hypotheses"] == []


def test_the_ablations_rank_every_root_and_the_report_holds_only_those_over_the_cut(
        engine_runs):
    for arm in ("engine_no_topology", "engine_no_explain_away"):
        run = engine_runs["hero", arm]
        conf = [r["conf"] for r in run["ranking"]]
        assert conf == sorted(conf, reverse=True) and run["ranking"]
        over = [h for h in run["report"]["hypotheses"]]
        assert [h["root_cause_uri"] for h in over] == [
            r["root"] for r in run["ranking"] if r["conf"] >= ev.MIN_CONFIDENCE][:len(over)]


def test_each_ablation_switches_one_gate_off(engine_runs):
    top = {r["root"] for r in engine_runs["hero", "engine_top"]["ranking"]}
    no_topology = {r["root"] for r in engine_runs["hero", "engine_no_topology"]["ranking"]}
    no_explain = {r["root"] for r in engine_runs["hero", "engine_no_explain_away"]["ranking"]}
    assert top <= no_topology and top <= no_explain
    assert no_topology != no_explain


# --- the LLM arms, on a fake model -------------------------------------------------------

def answering(report: dict, seen: list):
    def policy(messages, bound):
        seen.append(bound)
        return AIMessage("", tool_calls=[{"name": "RCAReport", "args": report, "id": "r"}])
    return policy


def test_the_graph_arms_differ_by_the_evidence_tool(eval_root, eval_uri):
    report = {"verdict": "inconclusive", "hypotheses": [], "summary": "stub"}
    offered = {}
    for arm in ("graph", "graph_no_evidence_tool"):
        seen = []
        run = run_one(RunSpec("hero", arm, 0), root=eval_root, evals_dir=eval_root / "evals",
                      model=fake_tool_model(answering(report, seen)))
        offered[arm] = set().union(*seen)
        assert run["report"]["summary"] == "stub" and run["model_calls"] == 1
        assert run["alert"] == ALERT and set(run) >= {"input_tokens", "output_tokens", "tools"}
    assert "candidate_evidence" in offered["graph"]
    assert "candidate_evidence" not in offered["graph_no_evidence_tool"]
    assert offered["graph"] - {"candidate_evidence"} == offered["graph_no_evidence_tool"]


def test_each_graph_run_starts_clean_and_leaves_no_state_behind(eval_root):
    from e2ekg.graph.store import Store
    graph = eval_root / "graph/hero/as_of/store"
    before = Store(graph, read_only=True).quad_count()
    listings = []

    def policy_for(i: int):
        def policy(messages, bound):
            done = [m for m in messages if isinstance(m, ToolMessage)]
            if not done:
                return AIMessage("", tool_calls=[{"name": "write_file", "id": "w", "args": {
                    "file_path": f"/evidence/note-{i}.md", "content": "n"}}])
            if len(done) == 1:
                return AIMessage("", tool_calls=[{"name": "ls", "args": {"path": "/evidence/"},
                                                  "id": "l"}])
            listings.append(done[-1].content)
            return AIMessage("", tool_calls=[{"name": "RCAReport", "id": "r", "args": {
                "verdict": "inconclusive", "hypotheses": [], "summary": "stub"}}])
        return policy
    for i in range(2):
        run_one(RunSpec("hero", "graph", i), root=eval_root, evals_dir=eval_root / "evals",
                model=fake_tool_model(policy_for(i)))
    assert "note-0.md" in listings[0]
    assert "note-1.md" in listings[1] and "note-0.md" not in listings[1]
    assert Store(graph, read_only=True).quad_count() == before
    assert not (eval_root / "agent").exists()


def test_the_raw_only_agent_reads_the_as_of_raw_files_and_cannot_write(eval_root):
    out = {}

    def policy(messages, bound):
        done = [m for m in messages if isinstance(m, ToolMessage)]
        if not done:
            calls = [("ls", {"path": "/raw/"}), ("grep", {"pattern": "radar-09000",
                                                           "path": "/raw/alerts"}),
                     ("write_file", {"file_path": "/raw/alerts/radar.jsonl", "content": "x"}),
                     ("edit_file", {"file_path": "/raw/events/properties.log",
                                    "old_string": "PropertyChanged", "new_string": "X"})]
            return AIMessage("", tool_calls=[{"name": n, "args": a, "id": f"{n}-{i}"}
                                             for i, (n, a) in enumerate(calls)])
        out.update({m.name: m.content for m in done}, tools=bound)
        return AIMessage("", tool_calls=[{"name": "RCAReport", "id": "r", "args": {
            "verdict": "inconclusive", "hypotheses": [], "summary": "stub"}}])
    run = run_one(RunSpec("hero", "raw_only", 0), root=eval_root, evals_dir=eval_root / "evals",
                  model=fake_tool_model(policy))
    assert run["report"]["summary"] == "stub"
    assert "events" in out["ls"] and "_answers" not in out["ls"]
    assert "radar.jsonl" in out["grep"]
    assert "read-only" in out["write_file"] and "read-only" in out["edit_file"]
    assert not out["tools"] & {"find_symptom", "causal_chains", "sparql_select", "task"}
    original = (eval_root / "raw/hero/events/properties.log").read_bytes()
    view = eval_root / "evals/_raw_asof/hero/events/properties.log"
    assert b"PropertyChanged" in view.read_bytes() and b"PropertyChanged" in original


def test_the_raw_only_agent_cannot_see_the_chat_written_after_the_cutoff(eval_root, preprocessed):
    import polars as pl
    pp = preprocessed("hero")
    cut = ev.case_for(eval_root, "hero").as_of
    slack = pl.read_parquet(pp.proc / "slack.parquet")
    late = slack.filter(pl.col("ts_utc") >= cut)
    assert late.height
    view = eval_root / "evals/_raw_asof/hero"
    if not view.is_dir():
        from e2ekg.evals.rawview import build_raw_view
        build_raw_view(pp.raw, pp.proc, cut, view)
    seen = b"".join(p.read_bytes() for p in (view / "slack").rglob("*") if p.is_file())
    assert seen, "the view holds no chat at all"
    for raw_id in late["raw_id"].to_list()[:50]:
        _, path, off, n, _ = raw_id.split("|")
        assert (pp.raw / path).read_bytes()[int(off):int(off) + int(n)] not in seen


# --- scoring the runs --------------------------------------------------------------------

def agent_run(scenario: str, arm: str, run: int, root: str | None, *, evidence=(),
              verdict="root_cause_found", failure: str | None = None) -> dict:
    report = ({"verdict": "inconclusive", "hypotheses": [], "summary": "x", "failure": failure}
              if failure else
              {"verdict": verdict, "summary": "x", "hypotheses": [] if root is None else [{
                  "root_cause_uri": root, "chain": [root], "confidence": 0.8,
                  "evidence": list(evidence), "refuted_alternatives": []}]})
    return {"scenario": scenario, "arm": arm, "run": run, "alert": "a", "seconds": 10.0,
            "input_tokens": 1000, "output_tokens": 10, "model_calls": 3, "report": report}


@pytest.fixture(scope="module")
def scored(eval_root, eval_uri, engine_runs):
    cfg, dep = eval_uri("hero", "chg-cfg-0001"), eval_uri("hero", "chg-dep-0001")
    bg = eval_uri("v-control", "chg-bg-0001")
    runs = [*engine_runs.values(),
            agent_run("hero", "graph", 0, cfg, evidence=[cfg]),
            agent_run("hero", "graph", 1, dep, evidence=[dep]),
            agent_run("hero", "graph", 2, None, failure="GraphRecursionError: x"),
            agent_run("hero", "raw_only", 0, None, verdict="inconclusive"),
            agent_run("v-control", "graph", 0, bg),
            agent_run("v-control", "graph", 1, None, verdict="inconclusive")]
    return score_runs(runs, eval_root)


def row(df, scenario, arm, run):
    return df.filter((df["scenario"] == scenario) & (df["arm"] == arm) & (df["run"] == run)
                     ).row(0, named=True)


def test_a_graph_run_naming_the_true_root_scores_top1_with_evidence_that_reaches_the_key(scored):
    r = row(scored, "hero", "graph", 0)
    assert r["top1"] and r["top3"] and not r["trap"] and r["true_root_rank"] == 1
    assert r["evidence_precision"] == 1.0


def test_a_graph_run_naming_a_herring_is_a_trap(scored):
    r = row(scored, "hero", "graph", 1)
    assert r["trap"] and not r["top1"] and r["true_root_rank"] is None


def test_a_failed_run_scores_as_a_failure_not_a_crash(scored):
    r = row(scored, "hero", "graph", 2)
    assert r["failed"] and r["failure"].startswith("GraphRecursion")
    assert not (r["top1"] or r["top3"] or r["trap"])


def test_the_control_is_a_false_positive_only_when_the_run_names_a_root(scored):
    assert row(scored, "v-control", "graph", 0)["control_false_positive"]
    abstained = row(scored, "v-control", "graph", 1)
    assert abstained["top1"] and not abstained["control_false_positive"]
    assert row(scored, "v-control", "engine_top", 0)["top1"]


def test_each_llm_run_is_compared_with_engine_top_on_its_scenario(scored):
    right, wrong, failed = (row(scored, "hero", "graph", i) for i in (0, 1, 2))
    assert right["agrees_with_engine"] and not right["beats_engine"] and not right["loses_engine"]
    assert not wrong["agrees_with_engine"] and wrong["loses_engine"] and not wrong["beats_engine"]
    assert not failed["agrees_with_engine"] and failed["loses_engine"]
    never = row(scored, "hero", "engine_top", 0)
    assert never["agrees_with_engine"] is None


def test_the_engine_ablation_rows_carry_the_true_roots_rank_cut_and_uncut(scored):
    for arm in ("engine_top", "engine_no_topology", "engine_no_explain_away"):
        r = row(scored, "hero", arm, 0)
        assert r["true_root_rank_uncut"] is not None and r["true_root_rank_uncut"] >= 1
    top = row(scored, "hero", "engine_top", 0)
    assert top["true_root_rank"] == top["true_root_rank_uncut"] == 1


def test_a_top1_root_carries_the_facts_the_flywheel_adapts_from(scored):
    r = row(scored, "hero", "graph", 0)
    assert (r["root_kind"], r["root_app"]) == (
        "config_change", "https://kg.local/app/application/title-metadata-svc")
    assert r["severity"] and r["incident_uri"]
    assert row(scored, "hero", "graph", 1)["root_kind"] is None


def test_run_evals_scores_what_finished_and_writes_the_summary(eval_root, tmp_path):
    run_dir, df, ex = run_evals(["hero", "v-control"], 1, ["engine_top", "engine_no_topology"],
                                root=eval_root, run_dir=tmp_path / "r1", say=lambda _: None)
    assert len(ex.done) == 4 and df.height == 4
    assert (run_dir / "results.parquet").is_file() and (run_dir / "meta.json").is_file()
    summary = (run_dir / "summary.md").read_text()
    assert "engine_top" in summary and "ScriptedClient" in summary and "## Arms" in summary
    again = run_evals(["hero", "v-control"], 1, ["engine_top", "engine_no_topology"],
                      root=eval_root, run_dir=run_dir, say=lambda _: None)
    assert again[2].done == [] and len(again[2].skipped) == 4
    assert len((run_dir / "invocations.jsonl").read_text().splitlines()) == 2
