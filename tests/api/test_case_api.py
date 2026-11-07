import json

import polars as pl
import pytest
from fastapi.testclient import TestClient

from e2ekg.api import app as app_module
from explain_helpers import engine_run as build_engine_run


@pytest.fixture(scope="module")
def run_report(harvested_hero_as_of, preprocessed_hero, scripted_llm):
    from agent_helpers import copy_store
    from e2ekg.agent.tools import make_tools
    h = harvested_hero_as_of
    tools = make_tools(copy_store(h.store), preprocessed_hero.proc, "hero", "api-test",
                       llm=scripted_llm)
    return build_engine_run(tools, h.symptom)


@pytest.fixture
def root(tmp_path, harvested_hero_as_of, preprocessed_hero, run_report):
    h = harvested_hero_as_of
    (tmp_path / "graph/hero").mkdir(parents=True)
    (tmp_path / "graph/hero/as_of").symlink_to(h.out)
    (tmp_path / "graph/hero/store").mkdir()  # _scenarios() lists a scenario by its full store
    (tmp_path / "processed").mkdir()
    (tmp_path / "processed/hero").symlink_to(preprocessed_hero.proc)
    (tmp_path / "raw").mkdir()  # raw_evidence reads the raw tree beside processed/ (no _answers)
    (tmp_path / "raw/hero").symlink_to(preprocessed_hero.raw)
    run = tmp_path / "evals/r1"
    (run / "runs").mkdir(parents=True)
    (run / "runs/hero__graph__0.json").write_text(json.dumps(run_report))
    (run / "meta.json").write_text(json.dumps({"chat": "Chat claims come from the ScriptedClient"}))
    pl.DataFrame([dict(scenario="hero", arm="graph", run=0, verdict="accepted", failed=False,
                       has_root=True, top1=True, top3=True, evidence_precision=0.5, trap=False,
                       control_false_positive=False, root_app="pay")]
                 ).write_parquet(run / "results.parquet")
    return tmp_path


@pytest.fixture
def client(root, monkeypatch):
    monkeypatch.setattr(app_module, "DATA", root)
    for f in (app_module._store, app_module._tools_cached):
        f.cache_clear()
    yield TestClient(app_module.app)
    for f in (app_module._store, app_module._tools_cached):
        f.cache_clear()


def test_case_default_source(client):
    r = client.get("/api/scenarios/hero/case")
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["scenario"] == "hero"
    assert any("root" in n["roles"] for n in body["nodes"])
    assert body["meta"]["chat_note"] == "claims extracted by scripted stand-in"


def test_case_unknown_source_404(client):
    r = client.get("/api/scenarios/hero/case", params={"source": "eval:nope"})
    assert r.status_code == 404
    assert client.get("/api/scenarios/hero/case", params={"source": "job:zzz"}).status_code == 404


def test_case_no_saved_run_404_names_command(client, root):
    import shutil
    shutil.rmtree(root / "evals")
    r = client.get("/api/scenarios/hero/case")
    assert r.status_code == 404
    assert "uv run e2ekg eval" in r.json()["detail"] and "AutoRCA" in r.json()["detail"]


def test_case_without_trail(client):
    body = client.get("/api/scenarios/hero/case").json()
    assert body["meta"]["has_trail"] is False and body["trail"] is None


def test_case_from_job(client, run_report):
    app_module.JOBS["j1"] = {"id": "j1", "scenario": "hero", "state": "done",
                             "alert": run_report["alert"], "report": run_report["report"]}
    try:
        r = client.get("/api/scenarios/hero/case", params={"source": "job:j1"})
        assert r.status_code == 200 and r.json()["meta"]["chat_note"] is None
        app_module.JOBS["j1"]["state"] = "running"
        assert client.get("/api/scenarios/hero/case", params={"source": "job:j1"}).status_code == 404
    finally:
        app_module.JOBS.pop("j1", None)


def test_lineage_endpoint(client):
    from e2ekg.graph.uris import NS
    rows = app_module._store("hero", "as_of").query(f"""SELECT ?c WHERE {{
        ?c a <{NS['ops']['ConfigChange']}> ; <{NS['chg']['property']}> ?p
        FILTER(STRENDS(?p, "minInstances")) }} LIMIT 1""")
    uri = str(next(iter(rows))["c"])
    r = client.get("/api/scenarios/hero/lineage", params={"uri": uri})
    assert r.status_code == 200
    assert r.json()["records"][0]["file"] == "events/properties.log"


def test_scorecard_endpoint(client):
    r = client.get("/api/scenarios/hero/scorecard")
    assert r.status_code == 200 and r.json()["arms"]
    assert client.get("/api/scenarios/hero/scorecard", params={"run": "nope"}).status_code == 404
