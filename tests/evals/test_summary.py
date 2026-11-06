"""The summary's tables and pass conditions, from hand-built scored runs."""
import polars as pl

from e2ekg.evals.summary import criteria, majority, render

SCENARIOS = ["hero", "a", "b", "c", "d", "e", "v-control"]


def rows(arm: str, hits: dict[str, list[bool]], **kw) -> list[dict]:
    out = []
    for sc, tops in hits.items():
        for i, top in enumerate(tops):
            out.append({"scenario": sc, "arm": arm, "run": i, "has_root": sc != "v-control",
                        "top1": top, "top3": top, "evidence_precision": 0.5 if top else 0.0,
                        "trap": kw.get("trap", False) and sc == "hero" and not top,
                        "control_false_positive": sc == "v-control" and not top, "failed": False,
                        "seconds": 100.0, "input_tokens": 500_000, "true_root_rank": 1 if top else None,
                        "true_root_rank_uncut": 1 if top else None, "n_hypotheses": 1,
                        "agrees_with_engine": top if arm != "engine_top" else None,
                        "beats_engine": False if arm != "engine_top" else None,
                        "loses_engine": not top if arm != "engine_top" else None})
    return out


def frame(*parts: list[dict]) -> pl.DataFrame:
    return pl.DataFrame([r for p in parts for r in p], infer_schema_length=None)


def test_a_scenario_counts_by_majority_only_when_more_than_half_the_runs_are_right():
    df = frame(rows("graph", {"hero": [True, True, False], "a": [True, False, False],
                               "b": [True, False], "c": [True]}))
    assert majority(df, "graph") == (2, 4)


def test_the_pass_conditions_follow_the_brief():
    good = {s: [True] * 3 if s == "v-control" else [True, True, False] for s in SCENARIOS}
    ok = criteria(frame(rows("graph", good), rows("raw_only", {s: [False] * 3 for s in SCENARIOS})))
    assert ok.count("PASS") == 4 and "not met" not in ok and "FAIL" not in ok
    bad = dict(good, hero=[False, False, False])
    out = criteria(frame(rows("graph", bad, trap=True)))
    assert "trap on hero: 3 (needs 0): FAIL" in out
    assert "control false positives: 0" in out and "by majority vote: 6/7" in out


def test_the_summary_names_every_arm_and_the_agreement_with_the_engine():
    df = frame(rows("graph", {s: [True] for s in SCENARIOS}),
               rows("engine_top", {s: [True] for s in SCENARIOS}))
    md = render(df, "t", {"model": "m", "git": {"commit": "abc"}, "runs": 1,
                          "chat": "ScriptedClient stand-in"}, None, [])
    for part in ("# Eval t", "| graph |", "| engine_top |", "same top-1 as engine_top",
                 "ScriptedClient", "## Pass conditions"):
        assert part in md


def test_an_eval_with_no_finished_run_says_so():
    assert "No finished runs" in render(pl.DataFrame(), "t", {}, None, [])
