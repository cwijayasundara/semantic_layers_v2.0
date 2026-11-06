"""The harvest: ordered enrichers, idempotent replay, SHACL, sorted Turtle,
the 12 namespaces, and every parsed row (so every answer-key evidence record)
reachable from the graph."""
import json

import polars as pl

from e2ekg.graph.enrichers.chat import ChatEnricher
from e2ekg.graph.harvest import (
    harvest, make_context, namespaces_in_use, raw_selectors, reachable_raw_ids, unreachable_rows,
)
from e2ekg.graph.uris import NS12
from e2ekg.preprocess.ledger import DIRECT_TABLES


def _ctx(proc, out, llm):
    return make_context("hero", proc, out, llm=llm)


def test_harvest_twice_is_identical(tmp_path, preprocessed_hero, scripted_llm):
    ctx = _ctx(preprocessed_hero.proc, tmp_path / "a", scripted_llm)
    a = harvest(ctx, out=tmp_path / "a", shacl=False)
    first = (tmp_path / "a" / "harvest.ttl").read_bytes()
    b = harvest(ctx, out=tmp_path / "a", shacl=False)          # same store, replayed
    assert a.quads == b.quads == ctx.store.quad_count()
    assert (tmp_path / "a" / "harvest.ttl").read_bytes() == first
    c = harvest(_ctx(preprocessed_hero.proc, tmp_path / "b", scripted_llm), out=tmp_path / "b",
                shacl=False)
    assert c.quads == a.quads                                    # a fresh store: byte-identical too
    assert (tmp_path / "b" / "harvest.ttl").read_bytes() == first


def test_shacl_clean(harvested_hero):
    assert harvested_hero.report.shacl_ok, harvested_hero.report.shacl_text[:3000]


def test_failed_enricher_degrades_not_blocks(tmp_path, preprocessed_hero, monkeypatch,
                                             scripted_llm):
    def boom(self, ctx):
        raise RuntimeError("chat source unavailable")
    monkeypatch.setattr(ChatEnricher, "fetch", boom)
    r = harvest(_ctx(preprocessed_hero.proc, tmp_path, scripted_llm), out=tmp_path, shacl=False)
    assert r.results["chat"].success is False and "unavailable" in r.results["chat"].error
    assert r.results["history"].success is True
    assert all(v.success for k, v in r.results.items() if k != "chat")


def test_all_twelve_namespaces_used(harvested_hero):
    assert namespaces_in_use(harvested_hero.store) >= set(NS12)


def test_harvest_writes_store_turtle_and_report(harvested_hero):
    assert (harvested_hero.out / "harvest.ttl").stat().st_size > 0
    assert (harvested_hero.out / "store").is_dir()
    report = json.loads((harvested_hero.out / "harvest.json").read_text())
    assert report["quads"] == harvested_hero.report.quads == harvested_hero.store.quad_count()
    assert report["shacl_ok"] is True and set(report["results"]) >= {"catalog", "history"}


def test_raw_evidence_reachable_from_graph(harvested_hero):
    answers = json.loads((harvested_hero.answers / "hero.json").read_text())
    reach = reachable_raw_ids(harvested_hero.store, harvested_hero.proc)
    missing = [r for r in answers["evidence_raw_ids"] if r not in reach]
    assert not missing, (len(missing), missing[:5])


def test_every_parsed_row_reachable_from_graph(harvested_hero):
    assert unreachable_rows(harvested_hero.store, harvested_hero.proc) == dict.fromkeys(
        ("events", "alerts", "pages", "incidents", "catalog", "slack", "logs", "metrics_atlas",
         "metrics_prom", "spans"), 0)


def test_cited_raw_ids_are_parsed_records(harvested_hero):
    ids, sels = raw_selectors(harvested_hero.store)
    known = set()
    for t in DIRECT_TABLES:
        known |= set(pl.read_parquet(harvested_hero.proc / f"{t}.parquet",
                                     columns=["raw_id"])["raw_id"])
    assert ids and ids <= known and sels


def test_cli_harvest(tmp_path, preprocessed_hero):
    from e2ekg.cli import main
    proc_root = preprocessed_hero.proc.parent
    assert main(["harvest", "--scenario", "hero", "--proc", str(proc_root),
                 "--out", str(tmp_path), "--llm", "scripted", "--no-shacl"]) == 0
    assert (tmp_path / "hero" / "harvest.ttl").exists()
    assert json.loads((tmp_path / "hero" / "harvest.json").read_text())["shacl_ok"] is None


def test_every_term_the_harvest_writes_is_declared(harvested_hero):
    """Predicates and classes come from the ontology (core, schemes, extensions) or W3C."""
    rows = harvested_hero.store.query("""SELECT DISTINCT ?term WHERE {
        { GRAPH ?g { ?s ?term ?o } } UNION { GRAPH ?g { ?s a ?term } }
        FILTER(?g != <https://kg.local/graph/ontology>)
        FILTER(!STRSTARTS(STR(?term), "http://www.w3.org/"))
        FILTER NOT EXISTS { GRAPH <https://kg.local/graph/ontology> { ?term ?p ?x } } }""")
    assert rows == []
