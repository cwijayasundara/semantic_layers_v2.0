"""An honest per-arm scorecard from a saved eval run (`results.parquet` + `meta.json`)."""
import json
from pathlib import Path

import polars as pl

from e2ekg.evals.arms import ARMS, ENGINE_ARMS

EVIDENCE_NOTE = "share of cited items that are in the answer key's must-cite list"


def _group(arm: str) -> str:
    return "engine" if arm in ENGINE_ARMS else "agent"


def _order(arm: str) -> tuple[int, str]:
    return (ARMS.index(arm), arm) if arm in ARMS else (len(ARMS), arm)


def _mean(s: pl.Series):
    return s.drop_nulls().mean() if s.drop_nulls().len() else None


def _arm_row(d: pl.DataFrame, arm: str) -> dict:
    # Failed runs score as misses, as in summary.md.
    miss = pl.col("failed").fill_null(False)
    d = d.with_columns(top1=pl.col("top1").fill_null(False) & ~miss,
                       top3=pl.col("top3").fill_null(False) & ~miss)
    hero, control = d.filter(pl.col("scenario") == "hero"), d.filter(~pl.col("has_root"))
    return {
        "arm": arm, "group": _group(arm), "scenarios": d["scenario"].n_unique(),
        "top1": _mean(d["top1"].cast(pl.Float64)), "top3": _mean(d["top3"].cast(pl.Float64)),
        "evidence_precision": _mean(d.filter(pl.col("has_root"))["evidence_precision"]),
        "trap_rate": _mean(hero["trap"].cast(pl.Float64)) if hero.height else None,
        "control_fp_rate": (_mean(control["control_false_positive"].cast(pl.Float64))
                            if control.height else None)}


def scorecard(run_dir: Path, scenario: str) -> dict:
    run_dir = Path(run_dir)
    df = pl.read_parquet(run_dir / "results.parquet")
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    arms = sorted(set(df["arm"]), key=_order)
    this = []
    for arm in arms:
        d = df.filter((pl.col("arm") == arm) & (pl.col("scenario") == scenario)).sort("run")
        if not d.height:
            continue
        last = d.row(d.height - 1, named=True)
        failed = d["failed"].fill_null(False)
        top1 = (d["top1"].fill_null(False) & ~failed).cast(pl.Float64).mean()
        this.append({"arm": arm, "group": _group(arm), "top1": top1 > 0.5,
                     "verdict": last["verdict"], "root_app": last["root_app"],
                     "trap": bool(last["trap"]), "evidence_precision": last["evidence_precision"]})
    return {"run": run_dir.name,
            "arms": [_arm_row(df.filter(pl.col("arm") == a), a) for a in arms],
            "this_scenario": this,
            "notes": {"evidence_precision": EVIDENCE_NOTE, "chat": meta.get("chat")}}
