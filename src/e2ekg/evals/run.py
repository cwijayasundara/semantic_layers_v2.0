"""Evaluation arms across the scenarios, resumable run by run (R53, R54).

    e2ekg eval --scenarios all --runs 3 --arms graph,graph_no_evidence_tool,raw_only,engine_top,...

Arms (all scored by the same `score()`):
  graph                   the AutoRCA agent with its evidence tool (`candidate_evidence`)
  graph_fast              the fast path (A.2): prefetch + one-shot specialists + synthesis;
                          opt-in (not in `all`), escalates to the Deep Path when weak
  graph_no_evidence_tool  the same agent without that tool
  raw_only                one agent over the as-of raw files, no graph (compare it with
                          graph_no_evidence_tool, not graph)
  engine_top              no LLM: the correlation engine's chains on the as-of store, in order
  engine_no_topology      the engine without its topology gate (beam 80, every root ranked)
  engine_no_explain_away  the engine without explaining away (the same)
The LLM arms are independent runs: each opens the as-of store read-only, works on a private
in-memory copy, and gets a fresh evidence directory. The engine arms are deterministic, so
they run once per scenario.

A run directory `data/evals/<UTC timestamp>/` holds `runs/<scenario>__<arm>__<run>.json`, one
file per finished run, written atomically. Invoking again with the same `--run-dir` skips what
is done, so an interrupted eval resumes. A run that raises (a network error, say) writes
nothing and is retried next time; a run that ends without a report is a finished run that
scored a failure. `summarize` scores the files into `results.parquet` and `summary.md`.
Only the scorer reads `_answers` (R22): the arms see the as-of store or the as-of raw view."""
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.language_models import BaseChatModel

from e2ekg.agent.autorca import FailedRCA, run_rca
from e2ekg.agent.fastpath import run_rca_fast
from e2ekg.agent.report import Hypothesis, RCAReport
from e2ekg.agent.tools import make_tools
from e2ekg.agent.trail import Trail
from e2ekg.correlate.engine import MIN_CONFIDENCE, correlate
from e2ekg.evals.arms import ARMS, DEFAULT_ARMS, ENGINE_ARMS, FAST_ARM, LLM_ARMS
from e2ekg.evals.rawview import ensure_raw_view
from e2ekg.evals.raw_only import run_raw_only
from e2ekg.evals.score import evidence_checker, load_answers, score, scenario_change
from e2ekg.graph.enrichers.correlation import chain_inputs
from e2ekg.graph.store import Store
from e2ekg.llm import MODEL_ID
from e2ekg.preprocess import asof

ENGINE_SWITCHES = {"engine_top": {}, "engine_no_topology": {"topology_gate": False},
                   "engine_no_explain_away": {"explain_away": False}}
ABLATION_BEAM = 80
CHAT_NOTE = ("Chat claims come from the ScriptedClient stand-in, not live GLM extraction "
             "(R54); a live-chat rebuild is a follow-up arm.")
RANKING_SHOWN = 40


@dataclass(frozen=True)
class RunSpec:
    scenario: str
    arm: str
    run: int

    @property
    def name(self) -> str:
        return f"{self.scenario}__{self.arm}__{self.run}"

    @property
    def llm(self) -> bool:
        return self.arm in (*LLM_ARMS, FAST_ARM)


@dataclass(frozen=True)
class Case:
    """What the harness knows about a scenario without the answer key."""
    scenario: str
    root: Path
    alert_id: str
    as_of: datetime

    @property
    def proc(self) -> Path:
        return self.root / "processed" / self.scenario

    @property
    def graph(self) -> Path:
        return self.root / "graph" / self.scenario / "as_of"

    def store(self) -> Store:
        return Store(self.graph / "store", read_only=True)


def scenarios_in(root: Path) -> list[str]:
    found = sorted(p.parent.parent.name for p in (Path(root) / "graph").glob("*/as_of/store"))
    return sorted(found, key=lambda s: (s != "hero", s))


def resolve_scenarios(root: Path, spec: str) -> list[str]:
    have = scenarios_in(root)
    if spec == "all":
        return have
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    missing = [s for s in wanted if s not in have]
    if missing:
        raise ValueError(f"no as-of store under {root}/graph for {', '.join(missing)}; "
                         f"have {', '.join(have) or '(none)'}")
    return wanted


def resolve_arms(spec: str) -> list[str]:
    arms = DEFAULT_ARMS if spec == "all" else tuple(a.strip() for a in spec.split(",") if a.strip())
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        raise ValueError(f"unknown arm {', '.join(unknown)}; choose from {', '.join(ARMS)}")
    return list(dict.fromkeys(arms))


def make_plan(scenarios: list[str], runs: int, arms: list[str]) -> list[RunSpec]:
    """Deterministic arms once, first; then the LLM arms run by run, so that a partial eval
    holds whole rounds over the scenarios."""
    plan = [RunSpec(s, a, 0) for s in scenarios for a in arms if a in ENGINE_ARMS]
    plan += [RunSpec(s, a, r) for r in range(runs) for s in scenarios for a in arms
             if a in (*LLM_ARMS, FAST_ARM)]
    return plan


def case_for(root: Path, scenario: str) -> Case:
    cut = asof.live_cutoff(Path(root) / "processed" / scenario)
    return Case(scenario, Path(root), cut.alert_id or cut.symptom.id, cut.as_of)


# --- the arms ----------------------------------------------------------------------------

class RunLog(UsageMetadataCallbackHandler):
    """Token usage, model calls and tool calls of one run."""

    def __init__(self):
        super().__init__()
        self.model_calls, self.tools = 0, {}

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.model_calls += 1

    def on_tool_start(self, serialized, input_str, **kwargs):
        name = (serialized or {}).get("name") or kwargs.get("name") or "?"
        self.tools[name] = self.tools.get(name, 0) + 1

    def tokens(self) -> dict[str, int]:
        use = self.usage_metadata.values()
        return {"input_tokens": sum(u.get("input_tokens", 0) for u in use),
                "output_tokens": sum(u.get("output_tokens", 0) for u in use)}


def symptom_of(case: Case, store: Store) -> dict:
    """find_symptom's answer for the case's alert: the symptom URI the engine explains and
    the incident it opened."""
    tools = make_tools(store, case.proc, case.scenario, "eval", as_of=case.as_of)
    return json.loads(tools["find_symptom"].invoke({"alert_id": case.alert_id}))


def engine_report(chains) -> RCAReport:
    kept = [c for c in chains if c.confidence >= MIN_CONFIDENCE]
    return RCAReport(
        verdict="root_cause_found" if kept else "inconclusive", summary="correlation engine",
        hypotheses=[Hypothesis(root_cause_uri=c.links[0].uri, chain=[s.uri for s in c.links],
                               confidence=c.confidence, evidence=[s.uri for s in c.links],
                               refuted_alternatives=[]) for c in kept])


def run_engine(case: Case, arm: str) -> dict:
    """The engine's chains on the as-of store. Ablations widen the beam to ABLATION_BEAM and
    keep every chain so the true root's rank survives the cut; the report holds the chains
    over the cut, in order."""
    store = case.store()
    symptom = symptom_of(case, store)["symptom_uri"]
    signals, edges, _ = chain_inputs(store, symptom)
    extra = {} if arm == "engine_top" else {"beam_width": ABLATION_BEAM}
    chains = correlate(signals, edges, symptom, min_confidence=0.0, **ENGINE_SWITCHES[arm], **extra)
    return {"report": engine_report(chains).model_dump(),
            "ranking": [{"root": c.links[0].uri, "conf": round(c.confidence, 4)}
                        for c in chains[:RANKING_SHOWN]]}


def run_agent(case: Case, arm: str, evals_dir: Path,
              model: str | BaseChatModel | None) -> dict:
    log, kw = RunLog(), ({"model": model} if model else {})
    if arm == "raw_only":
        raw = case.root / "raw" / case.scenario
        view = Path(evals_dir) / "_raw_asof" / case.scenario
        ensure_raw_view(raw, case.proc, case.as_of, view)
        report = run_raw_only(case.alert_id, view, callbacks=[log], **kw)
        return {"report": report.model_dump(), **log.tokens(), "model_calls": log.model_calls,
                "tools": log.tools}
    trail = Trail()
    escalated = False
    if arm == FAST_ARM:
        report = run_rca_fast(case.alert_id, case.scenario, root=case.root, callbacks=[log],
                              trail=trail, **kw)
        escalated = any(s["tool"] == "escalate" for s in trail.steps)
    else:
        report = run_rca(case.alert_id, case.scenario, root=case.root, callbacks=[log],
                         with_evidence_tool=arm == "graph", trail=trail, **kw)
    return {"report": report.model_dump(), "escalated": escalated, **log.tokens(),
            "model_calls": log.model_calls, "tools": log.tools, "trail": trail.dump()}


def run_one(spec: RunSpec, *, root: Path, evals_dir: Path,
            model: str | BaseChatModel | None = None) -> dict:
    case, t0 = case_for(root, spec.scenario), time.perf_counter()
    started = datetime.now(UTC)
    body = (run_engine(case, spec.arm) if not spec.llm
            else run_agent(case, spec.arm, evals_dir, model))
    return {"scenario": spec.scenario, "arm": spec.arm, "run": spec.run, "alert": case.alert_id,
            "as_of": case.as_of.isoformat(), "started_utc": started.isoformat(),
            "seconds": round(time.perf_counter() - t0, 1), **body}


# --- running and resuming ----------------------------------------------------------------

def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")   # processes may share a run dir
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    tmp.replace(path)


def new_run_dir(evals_dir: Path) -> Path:
    return Path(evals_dir) / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _git() -> dict:
    def run(*a):
        return subprocess.run(["git", *a], capture_output=True, text=True).stdout.strip()
    return {"commit": run("rev-parse", "--short", "HEAD"), "dirty": bool(run("status", "--short"))}


@dataclass
class Execution:
    done: list[str]
    skipped: list[str]
    errors: list[str]
    deferred: list[str]


def execute(plan: list[RunSpec], run_dir: Path, do: Callable[[RunSpec], dict], *,
            max_runs: int | None = None, say: Callable[[str], None] = print) -> Execution:
    """Run every spec of `plan` that has no file in <run_dir>/runs yet. `max_runs` caps the LLM
    runs started by this call (the deterministic arms are cheap and never held back)."""
    out, llm_started = Execution([], [], [], []), 0
    for i, spec in enumerate(plan, 1):
        target = Path(run_dir) / "runs" / f"{spec.name}.json"
        if target.exists():
            out.skipped.append(spec.name)
            continue
        if spec.llm and max_runs is not None and llm_started >= max_runs:
            out.deferred.append(spec.name)
            continue
        llm_started += spec.llm
        say(f"[{i}/{len(plan)}] {spec.name} ...")
        try:
            result = do(spec)
        except Exception as e:
            why = f"{type(e).__name__}: {e}"
            say(f"  FAILED to run ({why}); nothing saved, it is retried on the next invocation")
            with (Path(run_dir) / "errors.jsonl").open("a") as f:
                f.write(json.dumps({"run": spec.name, "error": why,
                                    "at": datetime.now(UTC).isoformat()}) + "\n")
            out.errors.append(spec.name)
            continue
        _write_json(target, result)
        out.done.append(spec.name)
        say(f"  {result['report']['verdict']} in {result['seconds']} s")
    return out


def run_evals(scenarios: list[str], runs: int, arms: list[str], *, root: Path = Path("data"),
              run_dir: Path | None = None, max_runs: int | None = None,
              model: str | BaseChatModel | None = None, say: Callable[[str], None] = print,
              do: Callable[[RunSpec], dict] | None = None) -> tuple[Path, pl.DataFrame, Execution]:
    """Run (or resume) the eval in `run_dir` (default: a new timestamped directory under
    <root>/evals), then score every finished run. `do` replaces the real runner in tests."""
    root = Path(root)
    evals_dir = root / "evals"
    run_dir = Path(run_dir) if run_dir else new_run_dir(evals_dir)
    plan = make_plan(scenarios, runs, arms)
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = run_dir / "meta.json"
    if not meta.exists():
        _write_json(meta, {"created_utc": datetime.now(UTC).isoformat(), "root": str(root),
                           "scenarios": scenarios, "runs": runs, "arms": arms,
                           "model": model or f"fireworks:{MODEL_ID}", "git": _git(),
                           "ablation_beam": ABLATION_BEAM, "chat": CHAT_NOTE})
    with (run_dir / "invocations.jsonl").open("a") as f:
        f.write(json.dumps({"at": datetime.now(UTC).isoformat(), "argv": sys.argv[1:],
                            "scenarios": scenarios, "runs": runs, "arms": arms,
                            "max_runs": max_runs}) + "\n")
    do = do or (lambda spec: run_one(spec, root=root, evals_dir=evals_dir, model=model))
    ex = execute(plan, run_dir, do, max_runs=max_runs, say=say)
    df = summarize(run_dir, root, plan)
    return run_dir, df, ex


# --- scoring -----------------------------------------------------------------------------

_PFX = ("PREFIX ops: <https://kg.local/ops#> PREFIX skos: <http://www.w3.org/2004/02/skos/core#> "
        "PREFIX inc: <https://kg.local/inc#> ")


def _root_facts(store: Store, uri: str) -> tuple[str | None, str | None]:
    """(change kind, application URI) of a change in the store. A raw-only report names the
    change by its bare external id, which is matched against the change node's last segment."""
    if uri.startswith(("http://", "https://")):
        node, where = f"<{uri}>", ""
    else:
        ext = json.dumps(uri.rsplit("/", 1)[-1])
        node, where = "?c", f' FILTER(STRENDS(STR(?c), CONCAT("/", {ext})))'
    rows = store.query(_PFX + f"SELECT ?kind ?app WHERE {{ {node} ops:changeKind/skos:notation "
                       f"?kind ; ops:changedApp ?app .{where} }}")
    return (rows[0]["kind"], rows[0]["app"]) if rows else (None, None)


class _Scorer:
    """Per scenario: the answer key, the as-of store, and the evidence checkers."""

    def __init__(self, root: Path, evals_dir: Path):
        self.root, self.evals_dir, self._memo = Path(root), Path(evals_dir), {}

    def scenario(self, name: str) -> dict:
        if name not in self._memo:
            case, answers = case_for(self.root, name), load_answers(self.root, name)
            store = case.store()
            view_dir = self.evals_dir / "_raw_asof" / name
            meta = view_dir.with_name(name + ".view.json")
            graph = evidence_checker(answers, store=store, proc=case.proc, as_of=case.as_of)
            raw = evidence_checker(answers, raw=view_dir,
                                   view=json.loads(meta.read_text())["segments"]) \
                if meta.is_file() else graph
            sym = symptom_of(case, store)
            self._memo[name] = {"answers": answers, "store": store, "graph": graph,
                                "raw": raw, "symptom": sym}
        return self._memo[name]


def load_runs(run_dir: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((Path(run_dir) / "runs").glob("*.json"))]


def _report(run: dict) -> RCAReport:
    r = run["report"]
    return FailedRCA.model_validate(r) if "failure" in r else RCAReport.model_validate(r)


def _rank(roots: list[str | None], key: str | None) -> int | None:
    return roots.index(key) + 1 if key in roots else None


def score_runs(runs: list[dict], root: Path, evals_dir: Path | None = None) -> pl.DataFrame:
    """One row per finished run: its score, the true root's rank, how it compares with
    engine_top, and the facts the flywheel needs of a top-1 root."""
    root = Path(root)
    scorer = _Scorer(root, evals_dir or root / "evals")
    rows = []
    for run in runs:
        sc = scorer.scenario(run["scenario"])
        answers, report = sc["answers"], _report(run)
        s = score(report, answers, sc["raw" if run["arm"] == "raw_only" else "graph"])
        key = answers["answer_key"]["root_cause_change_id"]
        named = [scenario_change(h.root_cause_uri, answers) for h in report.hypotheses]
        found = report.verdict == "root_cause_found"
        top = report.hypotheses[0].root_cause_uri if found and report.hypotheses else None
        ranked = [scenario_change(r["root"], answers) for r in run.get("ranking", [])]
        kind, app = _root_facts(sc["store"], top) if s.top1 and top and key else (None, None)
        incident = (sc["symptom"].get("incident") or {})
        rows.append({
            "scenario": run["scenario"], "arm": run["arm"], "run": run["run"],
            "alert": run["alert"], "verdict": report.verdict,
            "failed": isinstance(report, FailedRCA), "failure": getattr(report, "failure", None),
            "root_uri": top, "has_root": key is not None, "top1": s.top1, "top3": s.top3,
            "evidence_precision": s.evidence_precision, "trap": s.trap,
            "control_false_positive": s.control_false_positive,
            "true_root_rank": _rank(named, key) if found else None,
            "true_root_rank_uncut": _rank(ranked, key) if ranked else None,
            "n_hypotheses": len(report.hypotheses), "seconds": run["seconds"],
            "input_tokens": run.get("input_tokens"), "output_tokens": run.get("output_tokens"),
            "model_calls": run.get("model_calls"), "symptom_uri": sc["symptom"]["symptom_uri"],
            "incident_uri": incident.get("uri"), "severity": incident.get("severity") or None,
            "root_kind": kind, "root_app": app})
    df = pl.DataFrame(rows, schema_overrides={
        "root_uri": pl.String, "failure": pl.String, "true_root_rank": pl.Int64,
        "true_root_rank_uncut": pl.Int64, "input_tokens": pl.Int64, "output_tokens": pl.Int64,
        "model_calls": pl.Int64, "incident_uri": pl.String, "severity": pl.String,
        "root_kind": pl.String, "root_app": pl.String}) if rows else pl.DataFrame()
    return _versus_engine(df) if rows else df


def _versus_engine(df: pl.DataFrame) -> pl.DataFrame:
    """Per run of an LLM arm, against engine_top on the same scenario: same top-1 root (two
    abstentions agree), and which of the two got the root right."""
    eng = df.filter(pl.col("arm") == "engine_top").select(
        "scenario", engine_root=pl.col("root_uri"), engine_top1=pl.col("top1"))
    out = df.join(eng, on="scenario", how="left")
    llm = pl.col("arm").is_in(LLM_ARMS) & pl.col("engine_top1").is_not_null()
    return out.with_columns(
        agrees_with_engine=pl.when(llm).then(
            pl.col("root_uri").fill_null("").eq(pl.col("engine_root").fill_null(""))),
        beats_engine=pl.when(llm).then(pl.col("top1") & ~pl.col("engine_top1")),
        loses_engine=pl.when(llm).then(~pl.col("top1") & pl.col("engine_top1")))


def summarize(run_dir: Path, root: Path, plan: list[RunSpec] | None = None) -> pl.DataFrame:
    """Score the finished runs and write results.parquet and summary.md beside them."""
    from e2ekg.evals.summary import render
    run_dir = Path(run_dir)
    df = score_runs(load_runs(run_dir), root, run_dir.parent)
    meta = json.loads((run_dir / "meta.json").read_text()) if (run_dir / "meta.json").exists() else {}
    errors = [json.loads(line) for line in (run_dir / "errors.jsonl").read_text().splitlines()] \
        if (run_dir / "errors.jsonl").exists() else []
    if df.height:
        df.write_parquet(run_dir / "results.parquet")
    (run_dir / "summary.md").write_text(render(df, run_dir.name, meta, plan, errors))
    return df
