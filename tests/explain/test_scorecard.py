import json

import polars as pl
import pytest

from e2ekg.explain.scorecard import scorecard

CHAT = "Chat claims come from the ScriptedClient stand-in, not live GLM extraction."


def _row(scenario, arm, **kw):
    base = dict(scenario=scenario, arm=arm, run=0, verdict="accepted", failed=False,
                has_root=scenario != "v-control", top1=True, top3=True, evidence_precision=None,
                trap=False, control_false_positive=False, root_app="pay")
    return {**base, **kw}


@pytest.fixture
def run_dir(tmp_path):
    rows = [
        _row("hero", "graph", evidence_precision=0.5, trap=True),
        _row("v-control", "graph", top1=False, top3=False, control_false_positive=True),
        _row("hero", "engine_top", evidence_precision=1.0),
        _row("v-control", "engine_top", failed=True, top1=None, top3=None,
             control_false_positive=None),
    ]
    pl.DataFrame(rows).write_parquet(tmp_path / "results.parquet")
    (tmp_path / "meta.json").write_text(json.dumps({"chat": CHAT}))
    return tmp_path


def test_groups(run_dir):
    arms = {a["arm"]: a for a in scorecard(run_dir, "hero")["arms"]}
    assert arms["graph"]["group"] == "agent"
    assert arms["engine_top"]["group"] == "engine"
    assert [a["arm"] for a in scorecard(run_dir, "hero")["arms"]] == ["graph", "engine_top"]


def test_means(run_dir):
    arms = {a["arm"]: a for a in scorecard(run_dir, "hero")["arms"]}
    g, e = arms["graph"], arms["engine_top"]
    assert (g["scenarios"], g["top1"], g["top3"]) == (2, 0.5, 0.5)
    assert g["evidence_precision"] == 0.5 and g["trap_rate"] == 1.0
    assert g["control_fp_rate"] == 1.0
    assert e["top1"] == 0.5  # the failed row is a miss
    assert e["evidence_precision"] == 1.0 and e["trap_rate"] == 0.0
    assert e["control_fp_rate"] is None  # only control row failed, nulls skipped


def test_this_scenario_filter(run_dir):
    sc = scorecard(run_dir, "hero")
    assert [r["arm"] for r in sc["this_scenario"]] == ["graph", "engine_top"]
    assert sc["this_scenario"][0] == {
        "arm": "graph", "group": "agent", "top1": True, "verdict": "accepted",
        "root_app": "pay", "trap": True, "evidence_precision": 0.5}
    assert sc["run"] == run_dir.name


def test_note_copy(run_dir, tmp_path):
    notes = scorecard(run_dir, "hero")["notes"]
    assert notes["evidence_precision"] == (
        "share of cited items that are in the answer key's must-cite list")
    assert notes["chat"] == CHAT
    (run_dir / "meta.json").unlink()
    assert scorecard(run_dir, "hero")["notes"]["chat"] is None
