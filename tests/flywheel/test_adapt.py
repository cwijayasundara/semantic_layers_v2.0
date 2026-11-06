"""The flywheel: confirmed chains become ops:Patterns that the correlation priors read, and
nothing else does."""
import json

import polars as pl
import pytest
from agent_helpers import copy_store

from e2ekg.correlate.engine import PRIORS, correlate
from e2ekg.evals.run import RunSpec, run_one, score_runs
from e2ekg.flywheel.adapt import (
    FLYWHEEL_GRAPH, adapt, confirmed_chains, load_approvals, record_approval,
)
from e2ekg.graph.enrichers.correlation import chain_inputs, priors_with_history
from e2ekg.graph.store import Store

TMS = "https://kg.local/app/application/title-metadata-svc"


@pytest.fixture(scope="module")
def df(eval_root, eval_uri):
    """Scored runs on the fixture hero: engine_top (right), a graph run on the right root and
    a graph run on a red herring."""
    cfg, dep = eval_uri("hero", "chg-cfg-0001"), eval_uri("hero", "chg-dep-0001")

    def agent(run, root):
        return {"scenario": "hero", "arm": "graph", "run": run, "alert": "radar-09000",
                "seconds": 1.0, "report": {"verdict": "root_cause_found", "summary": "x",
                                           "hypotheses": [{"root_cause_uri": root, "chain": [root],
                                                           "confidence": 0.8, "evidence": [root],
                                                           "refuted_alternatives": []}]}}
    engine = run_one(RunSpec("hero", "engine_top", 0), root=eval_root, evals_dir=eval_root / "e")
    return score_runs([engine, agent(0, cfg), agent(1, dep)], eval_root)


@pytest.fixture
def history(harvested_hero):
    """The hero's full store, copied in memory so a test can write to it."""
    return copy_store(harvested_hero.store)


def approve(tmp_path, df, **kw):
    path = tmp_path / "approvals.jsonl"
    root = df.filter((pl.col("arm") == "graph") & pl.col("top1"))["root_uri"][0]
    record_approval("hero", root, "priya.nair", path)
    return path


def patterns(store):
    return store.query("PREFIX ops: <https://kg.local/ops#> SELECT ?p WHERE { ?p a ops:Pattern }")


def test_an_unapproved_correct_chain_creates_nothing(history, df, tmp_path):
    before = history.quad_count()
    assert df.filter(pl.col("top1") & pl.col("has_root")).height >= 2
    assert adapt(history, df, tmp_path / "no-such-approvals.jsonl") == []
    assert history.quad_count() == before


def test_approving_a_different_root_confirms_nothing(history, df, tmp_path, eval_uri):
    path = tmp_path / "a.jsonl"
    record_approval("hero", eval_uri("hero", "chg-dep-0001"), "priya.nair", path)
    assert adapt(history, df, path) == []
    record_approval("v-bad-deploy", eval_uri("hero", "chg-cfg-0001"), "priya.nair", path)
    assert adapt(history, df, path) == []


def test_an_approved_chain_that_was_not_top1_creates_nothing(history, df, tmp_path):
    wrong = df.filter(pl.col("arm") == "graph").filter(~pl.col("top1"))
    assert wrong.height == 1
    path = tmp_path / "a.jsonl"
    record_approval("hero", wrong["root_uri"][0], "priya.nair", path)
    assert adapt(history, wrong, path) == []


def test_an_approved_top1_chain_becomes_one_pattern_with_its_provenance(history, df, tmp_path):
    path = approve(tmp_path, df)
    (p,) = adapt(history, df, path)
    row = {r["p"]: r for r in history.query(f"""PREFIX ops: <https://kg.local/ops#>
        PREFIX prov: <http://www.w3.org/ns/prov#> PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
        SELECT ?p ?kind ?app ?sev ?n ?f ?inc WHERE {{ ?p a ops:Pattern ; ops:causeKind/skos:notation ?kind ;
          ops:onApplication ?app ; ops:severity/skos:notation ?sev ; ops:support ?n ;
          ops:frequency ?f ; prov:wasDerivedFrom ?inc . FILTER(?p = <{p}>) }}""")}[str(p)]
    assert (row["kind"], row["app"], row["n"]) == ("config_change", TMS, "1")
    assert row["sev"] == df.filter(pl.col("top1") & pl.col("has_root"))["severity"][0]
    assert row["inc"] == df["incident_uri"][0] and 0 < float(row["f"]) <= 1


def test_the_same_chain_from_two_arms_is_one_chain(history, df, tmp_path):
    path = approve(tmp_path, df)
    assert df.filter(pl.col("top1") & pl.col("has_root")).height == 2
    assert confirmed_chains(df, load_approvals(path)).height == 1


def test_adapting_twice_gives_the_same_graph(history, df, tmp_path):
    path = approve(tmp_path, df)
    first = adapt(history, df, path)
    quads = history.quad_count()
    assert adapt(history, df, path) == first and history.quad_count() == quads


def test_withdrawing_the_approval_withdraws_the_pattern(history, df, tmp_path):
    path = approve(tmp_path, df)
    adapt(history, df, path)
    path.write_text("")
    assert adapt(history, df, path) == []
    assert not [p for p in patterns(history) if "flywheel" in p["p"]]


def test_the_patterns_live_in_their_own_graph(history, df, tmp_path):
    path = approve(tmp_path, df)
    before = history.triples()
    adapt(history, df, path)
    added = history.triples() - before
    in_flywheel = history.triples([__import__("pyoxigraph").NamedNode(str(FLYWHEEL_GRAPH))])
    assert added == in_flywheel and added


def confirmed_row(df, i: int, kind: str, app: str = TMS) -> dict:
    return {**df.filter(pl.col("top1") & pl.col("has_root")).row(0, named=True),
            "scenario": f"s{i}", "root_uri": f"https://kg.local/chg/x/{i}", "root_kind": kind,
            "root_app": app}


def test_priors_with_history_reads_the_learned_pattern(history, df, tmp_path):
    """No scaling pattern in the hero's history, so the learned one is the only raise."""
    path, row = tmp_path / "a.jsonl", confirmed_row(df, 0, "scaling")
    record_approval(row["scenario"], row["root_uri"], "priya.nair", path)
    base = priors_with_history(history)
    assert base[("autoscaling", "metric_anomaly")] == PRIORS[("autoscaling", "metric_anomaly")]
    adapt(history, pl.DataFrame([row], schema=df.schema), path)
    learned = priors_with_history(history)
    assert learned[("autoscaling", "metric_anomaly")] > base[("autoscaling", "metric_anomaly")]
    assert learned[("deployment", "log_error")] == base[("deployment", "log_error")]


def test_adapt_does_not_flip_hero(history, df, tmp_path, harvested_hero, harvested_hero_as_of,
                                  eval_uri):
    """Patterns for every change kind, deployments on the herring's app included, leave the
    hero's top root where it was."""
    path = approve(tmp_path, df)
    extra = pl.DataFrame([confirmed_row(df, i, "deployment",
                                        "https://kg.local/app/application/api-gateway")
                          for i in range(40)], schema=df.schema)
    for i in range(40):
        record_approval(f"s{i}", f"https://kg.local/chg/x/{i}", "priya.nair", path)
    assert len(adapt(history, pl.concat([df, extra]), path)) == 2
    pri = priors_with_history(history)
    assert pri[("deployment", "log_error")] > priors_with_history(copy_store(harvested_hero.store))[
        ("deployment", "log_error")]
    top = correlate(*chain_inputs(harvested_hero_as_of.store, harvested_hero_as_of.symptom),
                    priors=pri)[0]
    assert top.links[0].uri == eval_uri("hero", "chg-cfg-0001")


def test_an_as_of_store_is_refused(tmp_path, df):
    graph = tmp_path / "graph" / "hero" / "as_of"
    (graph).mkdir(parents=True)
    (graph / "harvest.json").write_text(json.dumps({"as_of": "2026-10-14T23:00:00+00:00"}))
    store = Store(graph / "store")
    path = tmp_path / "a.jsonl"
    with pytest.raises(ValueError, match="as-of store"):
        adapt(store, df, path)
    assert store.quad_count() == 0


def test_a_full_store_on_disk_is_accepted(tmp_path, df):
    graph = tmp_path / "graph" / "hero"
    graph.mkdir(parents=True)
    (graph / "harvest.json").write_text(json.dumps({"as_of": None}))
    assert adapt(Store(graph / "store"), df, tmp_path / "none.jsonl") == []


def test_approvals_round_trip_and_a_malformed_line_is_an_error(tmp_path):
    path = tmp_path / "deep" / "approvals.jsonl"
    record_approval("hero", "u", "priya.nair", path)
    record_approval("hero", "v", "priya.nair", path)
    assert load_approvals(path) == {("hero", "u"), ("hero", "v")}
    assert json.loads(path.read_text().splitlines()[0])["approved_by"] == "priya.nair"
    with pytest.raises(ValueError, match="who approved"):
        record_approval("hero", "u", " ", path)
    path.write_text('{"scenario": "hero"}\n')
    with pytest.raises(ValueError, match=":1:"):
        load_approvals(path)
