"""The harvest: processed tables of one scenario -> the knowledge graph.

    ctx = make_context("hero", Path("data/processed/hero"), Path("data/graph/hero"))
    report = harvest(ctx)

The ontology (core, schemes, extensions) is loaded into its own graph, then the
ENRICHERS run in order, each replacing its own named graph (a rerun converges
to the same store), then SHACL validates the union, and the data graphs are
dumped as sorted Turtle to `<out>/harvest.ttl` with a JSON report beside it.
The store itself lives at `<out>/store`.

An as-of harvest (`make_context(..., as_of=t)`, R34) reads every processed table
through `e2ekg.preprocess.asof`, so its store holds only what was observable at t.
The live RCA reads that store; the full store serves the CQs, history and the
flywheel. The CLI writes it beside the full one, at `<out>/as_of`."""
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import polars as pl

from e2ekg.graph.enricher import EnrichmentResult, HarvestContext
from e2ekg.graph.enrichers import ENRICHERS
from e2ekg.graph.shacl import load_ontology, validate
from e2ekg.graph.store import Store
from e2ekg.graph.uris import BASE, NS12, ONTOLOGY_GRAPH
from e2ekg.preprocess.identity import Resolver, catalog_from_table
from e2ekg.preprocess.ledger import DIRECT_TABLES, SELECTED
from e2ekg.preprocess.selector import selected_raw_ids

DEFAULT_OUT = Path("data/graph")
RAW_SELECTOR = "https://kg.local/ops#rawSelector"


@dataclass(frozen=True)
class HarvestReport:
    results: dict[str, EnrichmentResult]
    shacl_ok: bool | None          # None: SHACL was not run
    quads: int
    shacl_text: str = ""


def make_context(scenario: str, proc_root: Path, out: Path, llm=None,
                 as_of: datetime | None = None) -> HarvestContext:
    proc_root = Path(proc_root)
    catalog = catalog_from_table(pl.read_parquet(proc_root / "catalog.parquet"))
    return HarvestContext(scenario=scenario, proc_root=proc_root, store=Store(Path(out) / "store"),
                          resolver=Resolver(catalog, llm), llm=llm, as_of=as_of)


def harvest(ctx: HarvestContext, out: Path | None = None, shacl: bool = True) -> HarvestReport:
    out = Path(out) if out is not None else DEFAULT_OUT / ctx.scenario
    ctx.store.replace_graph(ONTOLOGY_GRAPH, load_ontology())
    results = {e.name: e().run(ctx) for e in ENRICHERS}
    ok, text = validate(ctx.store) if shacl else (None, "")
    ctx.store.dump_turtle(out / "harvest.ttl")
    ctx.store.flush()
    report = HarvestReport(results=results, shacl_ok=ok, quads=ctx.store.quad_count(),
                           shacl_text=text)
    (out / "harvest.json").write_text(json.dumps(
        {"scenario": ctx.scenario, "as_of": ctx.as_of and ctx.as_of.isoformat(),
         "quads": report.quads, "shacl_ok": ok,
         "results": {k: asdict(v) for k, v in results.items()}}, indent=2) + "\n")
    return report


def namespaces_in_use(store: Store) -> set[str]:
    """NS12 namespaces of the data IRIs, as vocabulary (`<ns>#`) or instance (`<ns>/`)."""
    found = set()
    for s, p, o in store.triples():
        for t in (s, p, o):
            v = getattr(t, "value", "")
            if v.startswith(BASE + "/") and not hasattr(t, "datatype"):
                ns = v[len(BASE) + 1:].split("/", 1)[0].split("#", 1)[0]
                if ns in NS12:
                    found.add(ns)
    return found


def raw_selectors(store: Store) -> tuple[set[str], list[str]]:
    """(raw record ids, fingerprint selectors) the graph cites with ops:rawSelector."""
    rows = store.query(f"SELECT DISTINCT ?v WHERE {{ ?s <{RAW_SELECTOR}> ?v }}")
    ids = {r["v"] for r in rows if not r["v"].startswith("{")}
    return ids, sorted(r["v"] for r in rows if r["v"].startswith("{"))


def reachable_raw_ids(store: Store, proc_root: Path) -> set[str]:
    """Every raw record id the graph reaches: cited directly, or selected by a fingerprint."""
    ids, sels = raw_selectors(store)
    return ids | selected_raw_ids(proc_root, sels)


def unreachable_rows(store: Store, proc_root: Path) -> dict[str, int]:
    """Per parsed table, rows whose raw id the graph does not reach (all zero when complete)."""
    reach = reachable_raw_ids(store, proc_root)
    out = {}
    for t in (*DIRECT_TABLES, *(x for ts in SELECTED.values() for x in ts)):
        ids = pl.read_parquet(Path(proc_root) / f"{t}.parquet", columns=["raw_id"])["raw_id"]
        out[t] = int((~ids.is_in(list(reach))).sum())
    return out
