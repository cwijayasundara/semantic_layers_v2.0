"""`e2ekg` command line. Each task adds a subcommand via `_add_<name>`."""
import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

AS_OF_LIVE = "live"


def _scenarios(name: str, seed: int):
    from e2ekg.scenario.hero import hero_scenario
    from e2ekg.scenario.variants import variant_scenarios
    every = (hero_scenario(seed), *variant_scenarios(seed))
    if name == "all":
        return every
    picked = [s for s in every if s.name == name]
    if not picked:
        raise SystemExit(f"unknown scenario {name!r}; choose from "
                         f"{', '.join(s.name for s in every)} or all")
    return picked


def _cmd_generate(args: argparse.Namespace) -> int:
    from e2ekg.generate.run import generate_scenario
    for s in _scenarios(args.scenario, args.seed):
        t = time.perf_counter()
        m = generate_scenario(s, Path(args.out) / s.name, scale=args.scale,
                              attribution=args.attribution)
        print(f"{s.name}: {len(m.files)} files, {m.counts.get('logs', 0)} log records, "
              f"{m.counts.get('spans', 0)} spans, {len(m.injections)} injections "
              f"in {time.perf_counter() - t:.1f}s", flush=True)
    return 0


def _add_generate(sub) -> None:
    p = sub.add_parser("generate", help="render raw MELT data for a scenario")
    p.add_argument("--scenario", default="hero", help="hero, a v-... variant, or all")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", default="data/raw")
    p.add_argument("--scale", type=float, default=1.0,
                   help="background traffic multiplier for apps off the causal path")
    p.add_argument("--attribution", choices=("guess", "none", "correct"), default="guess",
                   help="the live incident's filed affected_services: guess (a wrong early "
                        "guess, corrected 40 min later; default), none (unattributed), or "
                        "correct (the true app from the start)")
    p.set_defaults(func=_cmd_generate)


def _raw_scenarios(raw: Path, name: str) -> list[str]:
    """Scenario dirs under the raw root; `_answers` and dot-dirs are not scenarios."""
    found = sorted(p.name for p in raw.iterdir() if p.is_dir() and p.name[0] not in "._") \
        if raw.is_dir() else []
    if name == "all":
        return found
    if name not in found:
        raise SystemExit(f"unknown scenario {name!r} under {raw}; choose from "
                         f"{', '.join(found) or '(none)'} or all")
    return [name]


def make_llm(kind: str):
    """`none` (no chat labels, no LLM identity), `scripted` (deterministic keyword rules, no
    network) or `fireworks` (GLM; needs FIREWORKS_API_KEY)."""
    import os
    if kind == "none":
        return None
    if kind == "scripted":
        from e2ekg.scripted_llm import ScriptedClient
        return ScriptedClient()
    if kind == "fireworks":
        if not os.environ.get("FIREWORKS_API_KEY"):
            raise SystemExit("--llm fireworks needs FIREWORKS_API_KEY in the environment")
        from e2ekg.llm import FireworksClient
        return FireworksClient()
    raise SystemExit(f"unknown --llm {kind!r}")


def _cmd_preprocess(args: argparse.Namespace) -> int:
    from e2ekg.preprocess.run import preprocess_scenario
    raw, out = Path(args.raw), Path(args.out)
    llm = make_llm(args.llm)
    for name in _raw_scenarios(raw, args.scenario):
        r = preprocess_scenario(raw / name, out / name, llm=llm)
        c = r.coverage
        print(f"{name}: {r.files} files, {sum(r.tables.values())} rows, "
              f"quarantine {r.quarantine}, coverage {c['bytes_covered']}/{c['bytes_total']} "
              f"bytes, gaps {c['gaps']}, overlaps {c['overlaps']}, "
              f"skewed hosts {{{', '.join(f'{h}: {v}' for h, v in r.skew['logs'].items() if v)}}} "
              f"in {r.seconds:.1f}s", flush=True)
    return 0


def _add_preprocess(sub) -> None:
    p = sub.add_parser("preprocess", help="lossless parse + time normalization + ledger")
    p.add_argument("--scenario", default="hero", help="hero, a v-... variant, or all")
    p.add_argument("--raw", default="data/raw")
    p.add_argument("--out", default="data/processed")
    p.add_argument("--llm", choices=("none", "scripted", "fireworks"), default="none",
                   help="chat labels and LLM identity picks; none leaves chat tables unbuilt")
    p.set_defaults(func=_cmd_preprocess)


# A few unclassified messages are expected (a failed batch falls back to single calls). The
# hero fixture classifies every message and its test requires 95%; past 5% the cause is
# systemic (bad key, rate limiting) and the chat tables are not evidence to reason from.
MAX_UNCLASSIFIED_FRAC = 0.05


def _cmd_ledger(args: argparse.Namespace) -> int:
    import json

    from e2ekg.preprocess.fingerprints import load_fingerprints
    from e2ekg.preprocess.ledger import check_coverage, check_reachability
    if args.proc:
        targets = [(Path(args.proc).name, Path(args.proc))]
    else:
        out = Path(args.out)
        names = sorted(p.name for p in out.iterdir() if (p / "ledger.json").exists()) \
            if out.is_dir() else []
        if args.scenario != "all":
            names = [n for n in names if n == args.scenario]
        if not names:
            raise SystemExit(f"no processed scenario {args.scenario!r} under {out}")
        targets = [(n, out / n) for n in names]
    bad = 0
    for name, proc in targets:
        problems: list[str] = []
        recorded = json.loads((proc / "ledger.json").read_text()) \
            if (proc / "ledger.json").exists() else {}
        raw = Path(args.raw) / name
        if raw.is_dir():
            c = check_coverage(raw, proc)
            print(f"{name}: coverage {c.bytes_covered}/{c.bytes_total} bytes, files {c.files}, "
                  f"gaps {len(c.gaps)}, overlaps {len(c.overlaps)}, "
                  f"mismatches {len(c.mismatches)}")
            if c.gaps or c.overlaps or c.mismatches or c.bytes_covered != c.bytes_total:
                problems.append("byte coverage")
        else:
            print(f"{name}: coverage skipped (no raw tree at {raw})")
        r = check_reachability(proc, load_fingerprints(proc))
        print(f"{name}: reachable {r.rows_reachable}/{r.rows_total} rows, "
              f"unreachable {len(r.unreachable_raw_ids)} raw ids, "
              f"phantom {len(r.phantom_raw_ids)}")
        if not r.ok:
            problems.append("reachability")
        import polars as pl
        for t, n in (recorded.get("tables") or {}).items():
            have = pl.scan_parquet(proc / f"{t}.parquet").select(pl.len()).collect().item()
            if have != n:
                problems.append(f"table {t}: {have} rows, ledger.json says {n}")
        chat = proc / "derived" / "chat_messages.parquet"
        if not chat.exists():
            msg = "chat outputs missing (run preprocess with --llm scripted|fireworks)"
            if args.allow_missing_chat:
                print(f"{name}: WARN {msg}")
            else:
                problems.append(msg)
        else:
            n_slack = pl.scan_parquet(proc / "slack.parquet").select(pl.len()).collect().item()
            n_chat = pl.scan_parquet(chat).select(pl.len()).collect().item()
            n_unc = pl.scan_parquet(chat).filter(pl.col("status") == "unclassified").select(
                pl.len()).collect().item()
            print(f"{name}: chat messages {n_chat}/{n_slack}, unclassified {n_unc}/{n_chat}")
            if n_chat != n_slack:
                problems.append(f"chat_messages has {n_chat} rows for {n_slack} slack rows")
            if n_unc > MAX_UNCLASSIFIED_FRAC * n_chat:
                problems.append(f"{n_unc}/{n_chat} chat messages unclassified "
                                f"(limit {MAX_UNCLASSIFIED_FRAC:.0%})")
        print(f"{name}: {'FAIL ' + '; '.join(problems) if problems else 'OK'}", flush=True)
        bad += bool(problems)
    return 1 if bad else 0


def _add_ledger(sub) -> None:
    p = sub.add_parser("ledger", help="byte coverage and fingerprint reachability proofs")
    p.add_argument("--scenario", default="hero", help="hero, a v-... variant, or all")
    p.add_argument("--out", default="data/processed", help="processed root")
    p.add_argument("--raw", default="data/raw", help="raw root (coverage is skipped if absent)")
    p.add_argument("--proc", default=None, help="one processed scenario directory")
    p.add_argument("--allow-missing-chat", action="store_true",
                   help="warn instead of fail when chat extraction outputs are missing "
                        "(a preprocess run with --llm none)")
    p.set_defaults(func=_cmd_ledger)


def _cmd_harvest(args: argparse.Namespace) -> int:
    from e2ekg.graph.harvest import harvest, make_context
    proc, out = Path(args.proc), Path(args.out)
    llm = make_llm(args.llm)
    bad = 0
    for name in _raw_scenarios(proc, args.scenario):
        t = time.perf_counter()
        as_of, dest = _as_of(args.as_of, proc / name), out / name
        if as_of is not None:
            dest = dest / "as_of"
        ctx = make_context(name, proc / name, dest, llm=llm, as_of=as_of)
        r = harvest(ctx, out=dest, shacl=not args.no_shacl)
        failed = sorted(k for k, v in r.results.items() if not v.success)
        shacl = {True: "clean", False: "VIOLATIONS", None: "skipped"}[r.shacl_ok]
        print(f"{name}: {r.quads} quads, SHACL {shacl}, failed enrichers "
              f"{', '.join(failed) or 'none'} in {time.perf_counter() - t:.1f}s", flush=True)
        for k in failed:
            print(f"  {k}: {r.results[k].error}")
        if r.shacl_ok is False:
            print(r.shacl_text[:4000])
        bad += bool(failed) or r.shacl_ok is False
    return 1 if bad else 0


def _as_of(value: str | None, proc: Path) -> datetime | None:
    """The --as-of cutoff: an ISO instant with a zone, or the live incident's (R21)."""
    from e2ekg.preprocess.asof import live_cutoff
    if value is None:
        return None
    if value == AS_OF_LIVE:
        c = live_cutoff(proc)
        print(f"{proc.name}: as of {c.as_of.isoformat()} ({c.symptom.kind} {c.symptom.id}: "
              f"alert {c.alert_id or '(page)'} at {c.alert_at.isoformat()} + 30 min)", flush=True)
        return c.as_of
    t = datetime.fromisoformat(value)
    if t.tzinfo is None:
        raise SystemExit(f"--as-of {value}: give a zone (e.g. 2026-10-14T23:00:00Z)")
    return t.astimezone(UTC)


def _add_harvest(sub) -> None:
    p = sub.add_parser("harvest", help="load processed tables into the knowledge graph")
    p.add_argument("--scenario", default="hero", help="hero, a v-... variant, or all")
    p.add_argument("--proc", default="data/processed", help="processed root")
    p.add_argument("--out", default="data/graph", help="graph root (<out>/<scenario>/store)")
    p.add_argument("--llm", choices=("none", "scripted", "fireworks"), default="none",
                   help="identity picks for names the catalog does not match outright")
    p.add_argument("--no-shacl", action="store_true", help="skip SHACL validation")
    p.add_argument("--as-of", nargs="?", const=AS_OF_LIVE, default=None, metavar="TS",
                   help="build the as-of store at <out>/<scenario>/as_of: only what was "
                        "observable at TS (no TS: the live incident's alert + 30 min, R21)")
    p.set_defaults(func=_cmd_harvest)


def _open_store(path: str):
    from e2ekg.graph.store import Store
    if not Path(path).is_dir():
        raise SystemExit(f"no harvested store at {path}; run `e2ekg harvest` first")
    return Store(Path(path))


def _verdict(path: Path, store, cq_dir: str | None) -> bool:
    from e2ekg.ontology_evolve.propose import check_proposal
    ok, report = check_proposal(path, store, Path(cq_dir) if cq_dir else None)
    print(f"{'ACCEPT' if ok else 'REJECT'} {path}\n{report}", flush=True)
    return ok


def _cmd_ontology_propose(args: argparse.Namespace) -> int:
    from e2ekg.ontology_evolve.propose import mine_candidates, propose
    store = _open_store(args.store)
    llm = make_llm(args.llm)
    found = mine_candidates(Path(args.proc), store)
    print(f"candidates: {len(found.error_templates)} error templates, {len(found.tag_keys)} "
          f"metric tag keys, {len(found.event_types)} event types", flush=True)
    made = propose(found, llm, Path(args.out))
    for p in made:
        print(f"\n{p.path}: {p.rationale}", flush=True)
        _verdict(p.path, store, args.cq_dir)
    print(f"\n{len(made)} proposals in {args.out}. Approval is manual: move an accepted file "
          "into ontology/extensions/.")
    return 0


def _cmd_ontology_check(args: argparse.Namespace) -> int:
    return 0 if _verdict(Path(args.proposal), _open_store(args.store), args.cq_dir) else 1


def _add_ontology(sub) -> None:
    p = sub.add_parser("ontology", help="LLM-proposed, test-gated ontology extensions")
    ps = p.add_subparsers(dest="ontology_command", required=True)
    gate = argparse.ArgumentParser(add_help=False)
    gate.add_argument("--store", default="data/graph/hero/store",
                      help="harvested store the SHACL and CQ gate runs against")
    gate.add_argument("--cq-dir", default=None,
                      help="competency questions to hold (default ontology/cq)")
    q = ps.add_parser("propose", parents=[gate],
                      help="mine what the ontology does not cover and have the LLM propose "
                           "extensions, each gated")
    q.add_argument("--proc", default="data/processed/hero", help="processed scenario directory")
    q.add_argument("--out", default="ontology/proposals")
    q.add_argument("--llm", choices=("fireworks",), default="fireworks")
    q.set_defaults(func=_cmd_ontology_propose)
    c = ps.add_parser("check", parents=[gate], help="gate one proposal file; exit 1 if rejected")
    c.add_argument("proposal")
    c.set_defaults(func=_cmd_ontology_check)


def _cmd_rca(args: argparse.Namespace) -> int:
    if args.fast:
        from e2ekg.agent.fastpath import run_rca_fast
        report = run_rca_fast(args.alert, args.scenario, root=Path(args.root),
                              evidence_root=Path(args.evidence) if args.evidence else None)
    else:
        from e2ekg.agent.autorca import run_rca
        report = run_rca(args.alert, args.scenario, root=Path(args.root),
                         evidence_root=Path(args.evidence) if args.evidence else None)
    print(report.model_dump_json(indent=2))
    return 0 if report.verdict == "root_cause_found" else 1


def _add_rca(sub) -> None:
    p = sub.add_parser("rca", help="AutoRCA: investigate an alert over the as-of graph (GLM; "
                                   "needs FIREWORKS_API_KEY)")
    p.add_argument("--scenario", default="hero")
    p.add_argument("--alert", required=True, help="alert id (radar-...) or incident id")
    p.add_argument("--root", default="data", help="data root (<root>/graph/<scenario>/as_of)")
    p.add_argument("--evidence", default=None,
                   help="directory behind the agent's /evidence/, kept after the run "
                        "(default: a temporary directory per run)")
    p.add_argument("--fast", action="store_true",
                   help="the fast path (A.2): prefetch + one-shot specialists + synthesis; "
                        "escalates to the Deep Path when the synthesis is weak")
    p.set_defaults(func=_cmd_rca)


def _cmd_eval(args: argparse.Namespace) -> int:
    from e2ekg.evals.run import resolve_arms, resolve_scenarios, run_evals, summarize
    root = Path(args.root)
    try:
        scenarios = resolve_scenarios(root, args.scenarios)
        arms = resolve_arms(args.arms)
    except ValueError as e:
        raise SystemExit(str(e)) from e
    if args.summarize_only:
        if not args.run_dir:
            raise SystemExit("--summarize-only needs --run-dir")
        summarize(Path(args.run_dir), root)
        run_dir, errors = Path(args.run_dir), 0
    else:
        run_dir, _, ex = run_evals(scenarios, args.runs, arms, root=root,
                                   run_dir=Path(args.run_dir) if args.run_dir else None,
                                   max_runs=args.max_runs, model=args.model)
        errors = len(ex.errors)
        print(f"{len(ex.done)} run, {len(ex.skipped)} already done, {len(ex.deferred)} left "
              f"for --max-runs, {errors} raised", flush=True)
    print(f"run dir {run_dir}\n{(run_dir / 'summary.md').read_text()}")
    return 1 if errors else 0


def _add_eval(sub) -> None:
    from e2ekg.evals.arms import ARMS
    p = sub.add_parser("eval", help="score the RCA arms across the scenarios (resumable; the "
                                    "LLM arms need FIREWORKS_API_KEY)")
    p.add_argument("--scenarios", default="all", help="comma list of scenarios, or all")
    p.add_argument("--runs", type=int, default=3, help="runs per LLM arm and scenario")
    p.add_argument("--arms", default="all", help=f"comma list of {', '.join(ARMS)}, or all")
    p.add_argument("--root", default="data", help="data root (graph/, processed/, raw/)")
    p.add_argument("--run-dir", default=None,
                   help="resume this run directory (default: a new <root>/evals/<timestamp>)")
    p.add_argument("--max-runs", type=int, default=None,
                   help="stop after this many LLM runs in this invocation")
    p.add_argument("--model", default=None, help="chat model (default: the project's GLM)")
    p.add_argument("--summarize-only", action="store_true",
                   help="rescore and rewrite the summary of --run-dir without running anything")
    p.set_defaults(func=_cmd_eval)


def _cmd_approve(args: argparse.Namespace) -> int:
    from e2ekg.flywheel.adapt import APPROVALS, record_approval
    path = Path(args.file) if args.file else APPROVALS
    record_approval(args.scenario, args.root_uri, args.by, path)
    print(f"recorded {args.by}'s approval of {args.root_uri} for {args.scenario} in {path}")
    return 0


def _cmd_adapt(args: argparse.Namespace) -> int:
    import polars as pl

    from e2ekg.flywheel.adapt import APPROVALS, adapt
    results = Path(args.run_dir) / "results.parquet"
    if not results.is_file():
        raise SystemExit(f"{results} not found; run `e2ekg eval --summarize-only` first")
    made = adapt(_open_store(args.store), pl.read_parquet(results),
                 Path(args.approvals) if args.approvals else APPROVALS)
    print(f"{len(made)} patterns written to {args.store}")
    for u in made:
        print(f"  {u}")
    return 0


def _add_flywheel(sub) -> None:
    p = sub.add_parser("approve", help="record a person's approval of a root cause, which lets "
                                       "a top-1 correct chain become a pattern")
    p.add_argument("--scenario", required=True)
    p.add_argument("--root-uri", required=True, help="the change URI the report named")
    p.add_argument("--by", required=True, help="who approved it")
    p.add_argument("--file", default=None, help="approvals file (default data/evals/approvals.jsonl)")
    p.set_defaults(func=_cmd_approve)
    q = sub.add_parser("adapt", help="write ops:Patterns for the approved, top-1 correct chains "
                                     "of an eval into a history store (never an as-of store)")
    q.add_argument("--run-dir", required=True, help="an eval run directory with results.parquet")
    q.add_argument("--store", required=True, help="the history store to write, e.g. "
                                                  "data/graph/hero/store")
    q.add_argument("--approvals", default=None)
    q.set_defaults(func=_cmd_adapt)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="e2ekg")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_generate(sub)
    _add_preprocess(sub)
    _add_ledger(sub)
    _add_harvest(sub)
    _add_ontology(sub)
    _add_rca(sub)
    _add_eval(sub)
    _add_flywheel(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
