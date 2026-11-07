"""The markdown summary of an eval: tables from the scored runs (`run.score_runs`)."""
import polars as pl

from e2ekg.evals.arms import ARMS, ENGINE_ARMS, LLM_ARMS


def _arms(df: pl.DataFrame) -> list[str]:
    have = set(df["arm"])
    return [a for a in ARMS if a in have]


def _pct(x) -> str:
    return "-" if x is None else f"{100 * x:.0f}%"


def _table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join("" if c is None else str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _per_scenario(df: pl.DataFrame, arm: str) -> pl.DataFrame:
    """scenario, runs, top1 hits, top3 hits, traps, failed."""
    return df.filter(pl.col("arm") == arm).group_by("scenario").agg(
        runs=pl.len(), top1=pl.col("top1").sum(), top3=pl.col("top3").sum(),
        traps=pl.col("trap").sum(), fps=pl.col("control_false_positive").sum(),
        failed=pl.col("failed").sum()).sort("scenario")


def majority(df: pl.DataFrame, arm: str) -> tuple[int, int]:
    """(scenarios whose top-1 is right in more than half the arm's runs, scenarios)."""
    per = _per_scenario(df, arm)
    return int((per["top1"] * 2 > per["runs"]).sum()), per.height


def overall(df: pl.DataFrame) -> str:
    rows = []
    for arm in _arms(df):
        d = df.filter(pl.col("arm") == arm)
        wins, n = majority(df, arm)
        fault = d.filter(pl.col("has_root"))
        hero, control = d.filter(pl.col("scenario") == "hero"), d.filter(~pl.col("has_root"))
        rows.append([
            arm, n, d.height, _pct(d["top1"].mean()), f"{wins}/{n}", _pct(d["top3"].mean()),
            _pct(fault["evidence_precision"].mean()) if fault.height else "-",
            f"{hero['trap'].sum()}/{hero.height}" if hero.height else "-",
            f"{control['control_false_positive'].sum()}/{control.height}" if control.height else "-",
            int(d["failed"].sum()),
            "-" if arm in ENGINE_ARMS else f"{d['seconds'].median():.0f}",
            "-" if arm in ENGINE_ARMS else f"{(d['input_tokens'].mean() or 0) / 1000:.0f}k"])
    return _table(["arm", "scenarios", "runs", "top1", "top1 by majority", "top3",
                   "evidence precision", "hero trap", "control false positive", "failed",
                   "median s", "mean input tokens"], rows)


def pivot(df: pl.DataFrame, column: str, label: str) -> str:
    arms, scenarios = _arms(df), sorted(set(df["scenario"]), key=lambda s: (s != "hero", s))
    rows = []
    for sc in scenarios:
        row = [sc]
        for arm in arms:
            p = _per_scenario(df, arm).filter(pl.col("scenario") == sc)
            row.append(f"{p[column][0]}/{p['runs'][0]}" if p.height else "")
        rows.append(row)
    return f"{label}\n\n" + _table(["scenario", *arms], rows)


def versus_engine(df: pl.DataFrame) -> str:
    rows = []
    for arm in [a for a in LLM_ARMS if a in set(df["arm"])]:
        d = df.filter((pl.col("arm") == arm) & pl.col("agrees_with_engine").is_not_null())
        if not d.height:
            continue
        rows.append([arm, d.height, _pct(d["agrees_with_engine"].mean()),
                     int(d["beats_engine"].sum()), int(d["loses_engine"].sum()),
                     int((d["top1"] & ~d["beats_engine"]).sum()),
                     int((~d["top1"] & ~d["loses_engine"]).sum())])
    return _table(["agent arm", "runs", "same top-1 as engine_top", "beats engine_top",
                   "loses to engine_top", "both right", "both wrong"], rows)


def ranks(df: pl.DataFrame) -> str:
    rows = []
    for sc in sorted(set(df["scenario"]), key=lambda s: (s != "hero", s)):
        d = df.filter((pl.col("scenario") == sc) & pl.col("has_root"))
        if not d.height:
            continue
        row = [sc]
        for arm in ENGINE_ARMS:
            r = d.filter(pl.col("arm") == arm)
            if not r.height:
                row.append("")
                continue
            kept, uncut = r["true_root_rank"][0], r["true_root_rank_uncut"][0]
            where = str(kept) if kept else f"under the cut (uncut {uncut})" if uncut else "absent"
            row.append(f"{where}; {r['n_hypotheses'][0]} chains over the cut")
        rows.append(row)
    return _table(["scenario", *ENGINE_ARMS], rows) if rows else "(no engine arm ran)"


def comparisons(df: pl.DataFrame) -> str:
    lines = []
    have = set(df["arm"])
    for a, b, why in (("graph", "graph_no_evidence_tool", "what the evidence tool adds"),
                      ("graph_no_evidence_tool", "raw_only", "what the graph adds (R53's pair)"),
                      ("graph", "raw_only", "the brief's pair")):
        if a in have and b in have:
            ma, mb = (df.filter(pl.col("arm") == x)["top1"].mean() for x in (a, b))
            wa, na = majority(df, a)
            wb, nb = majority(df, b)
            lines.append(f"- {a} vs {b} ({why}): top1 {_pct(ma)} vs {_pct(mb)}, by majority "
                         f"{wa}/{na} vs {wb}/{nb}.")
    return "\n".join(lines) or "(fewer than two LLM arms ran)"


def criteria(df: pl.DataFrame) -> str:
    """The program's pass conditions (the brief's step 5, with R53's pairing)."""
    if "graph" not in set(df["arm"]):
        return "(graph arm not run)"
    wins, n = majority(df, "graph")
    g = df.filter(pl.col("arm") == "graph")
    hero_traps = int(g.filter(pl.col("scenario") == "hero")["trap"].sum())
    fps = int(g.filter(~pl.col("has_root"))["control_false_positive"].sum())
    out = [f"- graph top-1 by majority vote: {wins}/{n} (needs 6 of 7): "
           f"{'PASS' if wins >= 6 and n >= 7 else 'not met'}",
           f"- graph trap on hero: {hero_traps} (needs 0): {'PASS' if hero_traps == 0 else 'FAIL'}",
           f"- graph control false positives: {fps} (needs 0): {'PASS' if fps == 0 else 'FAIL'}"]
    for other in ("raw_only", "graph_no_evidence_tool"):
        if other in set(df["arm"]):
            a, b = g["top1"].mean(), df.filter(pl.col("arm") == other)["top1"].mean()
            out.append(f"- graph top1 above {other}: {_pct(a)} vs {_pct(b)}: "
                       f"{'PASS' if a > b else 'not met'}")
    return "\n".join(out)


def render(df: pl.DataFrame, name: str, meta: dict, plan, errors: list[dict]) -> str:
    head = [f"# Eval {name}", ""]
    if meta:
        head += [f"- model {meta.get('model')}, commit {meta.get('git', {}).get('commit')}"
                 f"{' (dirty)' if meta.get('git', {}).get('dirty') else ''}, runs per LLM arm "
                 f"{meta.get('runs')}, ablation beam {meta.get('ablation_beam')}",
                 f"- {meta.get('chat')}"]
    if plan:
        have = {(r["scenario"], r["arm"], r["run"]) for r in df.iter_rows(named=True)} \
            if df.height else set()
        left = [p.name for p in plan if (p.scenario, p.arm, p.run) not in have]
        head.append(f"- runs finished {len(plan) - len(left)} of {len(plan)}"
                    + (f"; not yet run: {len(left)}" if left else ""))
    if errors:
        head.append(f"- {len(errors)} attempts raised and saved nothing; the last: "
                    f"{errors[-1]['run']}: {errors[-1]['error']}")
    if not df.height:
        return "\n".join([*head, "", "No finished runs."])
    body = [
        "## Arms", overall(df),
        "top1 and top3 are means over runs; a control run is right when it abstains. "
        "Evidence precision is the mean over fault scenarios. Failed runs score as misses.",
        "## What the agent adds", comparisons(df),
        "## Agent against the engine_top baseline", versus_engine(df),
        "Same top-1 counts two abstentions as agreeing. Engine arms run once per scenario.",
        "## Per scenario", pivot(df, "top1", "Top-1 right (runs)"),
        pivot(df, "top3", "Top-3 right (runs)"),
        pivot(df, "traps", "Red herring first (runs)"),
        pivot(df, "failed", "Failed runs"),
        "## True root's rank in the engine arms", ranks(df),
        "Rank among the chains over the 0.6 cut (`under the cut`: the root has a chain, but below "
        "0.6). Uncut is the rank with the cut lowered to 0 (`absent`: no chain roots at the true "
        "change at all, e.g. an earlier unrelated cascade explained it away). Ablations search "
        "with a beam of 80, wide enough that 300 and 1000 gave the same ranks.",
        "## Pass conditions", criteria(df)]
    return "\n\n".join(["\n".join(head), *body]) + "\n"
