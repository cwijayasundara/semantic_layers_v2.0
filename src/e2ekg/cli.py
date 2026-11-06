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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="e2ekg")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_generate(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
