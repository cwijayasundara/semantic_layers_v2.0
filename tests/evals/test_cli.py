"""`e2ekg eval`, `e2ekg approve` and `e2ekg adapt` on the command line."""
import json

import polars as pl
import pytest

from e2ekg.cli import build_parser, main
from e2ekg.evals import run as ev
from e2ekg.evals.run import Execution


def test_eval_defaults_run_every_arm_three_times_over_every_scenario():
    a = build_parser().parse_args(["eval"])
    assert (a.scenarios, a.runs, a.arms, a.max_runs, a.run_dir) == ("all", 3, "all", None, None)


def test_eval_passes_its_flags_to_the_runner_and_prints_the_summary(tmp_path, monkeypatch, capsys):
    (tmp_path / "graph/hero/as_of/store").mkdir(parents=True)
    (tmp_path / "graph/v-control/as_of/store").mkdir(parents=True)
    seen = {}

    def fake(scenarios, runs, arms, **kw):
        seen.update(scenarios=scenarios, runs=runs, arms=arms, **kw)
        run_dir = tmp_path / "evals" / "t"
        run_dir.mkdir(parents=True)
        (run_dir / "summary.md").write_text("# the summary\n")
        return run_dir, pl.DataFrame(), Execution(["a"], ["b", "c"], [], ["d"])
    monkeypatch.setattr(ev, "run_evals", fake)
    code = main(["eval", "--root", str(tmp_path), "--scenarios", "hero", "--runs", "1",
                 "--arms", "graph,engine_top", "--max-runs", "4"])
    assert code == 0 and seen["scenarios"] == ["hero"] and seen["runs"] == 1
    assert seen["arms"] == ["graph", "engine_top"] and seen["max_runs"] == 4
    out = capsys.readouterr().out
    assert "# the summary" in out and "1 run, 2 already done, 1 left" in out


def test_eval_exits_nonzero_when_a_run_raised(tmp_path, monkeypatch):
    (tmp_path / "graph/hero/as_of/store").mkdir(parents=True)
    run_dir = tmp_path / "t"
    run_dir.mkdir()
    (run_dir / "summary.md").write_text("x")
    monkeypatch.setattr(ev, "run_evals", lambda *a, **k: (run_dir, pl.DataFrame(),
                                                          Execution([], [], ["x"], [])))
    assert main(["eval", "--root", str(tmp_path), "--scenarios", "hero"]) == 1


def test_eval_refuses_an_unknown_arm_or_scenario_by_name(tmp_path):
    (tmp_path / "graph/hero/as_of/store").mkdir(parents=True)
    with pytest.raises(SystemExit, match="no_gate"):
        main(["eval", "--root", str(tmp_path), "--arms", "no_gate"])
    with pytest.raises(SystemExit, match="v-nope"):
        main(["eval", "--root", str(tmp_path), "--scenarios", "v-nope"])


def test_approve_appends_to_the_approvals_file(tmp_path, capsys):
    f = tmp_path / "approvals.jsonl"
    assert main(["approve", "--scenario", "hero", "--root-uri", "u", "--by", "priya.nair",
                 "--file", str(f)]) == 0
    assert json.loads(f.read_text())["scenario"] == "hero"


def test_adapt_needs_a_scored_run_dir(tmp_path):
    with pytest.raises(SystemExit, match="summarize-only"):
        main(["adapt", "--run-dir", str(tmp_path), "--store", str(tmp_path / "s")])
