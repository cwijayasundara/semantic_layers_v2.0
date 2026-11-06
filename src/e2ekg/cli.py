"""`e2ekg` command line. Each task adds a subcommand via `_add_<name>`."""
import argparse
import sys
import time
from pathlib import Path


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
        m = generate_scenario(s, Path(args.out) / s.name, scale=args.scale)
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


def _cmd_preprocess(args: argparse.Namespace) -> int:
    from e2ekg.preprocess.run import preprocess_scenario
    raw, out = Path(args.raw), Path(args.out)
    for name in _raw_scenarios(raw, args.scenario):
        r = preprocess_scenario(raw / name, out / name, llm=None)
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
    p.set_defaults(func=_cmd_preprocess)


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
        print(f"{name}: {'FAIL ' + '; '.join(problems) if problems else 'OK'}", flush=True)
        bad += bool(problems)
    return 1 if bad else 0


def _add_ledger(sub) -> None:
    p = sub.add_parser("ledger", help="byte coverage and fingerprint reachability proofs")
    p.add_argument("--scenario", default="hero", help="hero, a v-... variant, or all")
    p.add_argument("--out", default="data/processed", help="processed root")
    p.add_argument("--raw", default="data/raw", help="raw root (coverage is skipped if absent)")
    p.add_argument("--proc", default=None, help="one processed scenario directory")
    p.set_defaults(func=_cmd_ledger)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="e2ekg")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_generate(sub)
    _add_preprocess(sub)
    _add_ledger(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
