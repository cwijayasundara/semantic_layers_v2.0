"""The as-of store (R34): only what was observable at the live incident's cutoff."""
import json
from datetime import UTC, datetime

import polars as pl
import pytest

from e2ekg.graph.harvest import raw_selectors
from e2ekg.preprocess.asof import AS_OF_AFTER_ALERT, Symptom, live_cutoff, scan
from e2ekg.preprocess.ledger import DIRECT_TABLES

PFX = """PREFIX ops: <https://kg.local/ops#>
PREFIX inc: <https://kg.local/inc#>
PREFIX trc: <https://kg.local/trc#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""
API_GATEWAY = "https://kg.local/app/application/api-gateway"


def q(store, body: str):
    return store.query(PFX + body)


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_the_cutoff_is_the_live_alert_plus_30_minutes(preprocessed_hero):
    answers = json.loads((preprocessed_hero.answers / "hero.json").read_text())
    c = live_cutoff(preprocessed_hero.proc)
    assert (c.symptom, c.alert_id) == (Symptom("incident", "INC-5377"), "radar-09000")
    assert c.as_of == _t(answers["as_of_utc"])


def _raw_firings(raw) -> list[dict]:
    rows = []
    for line in (raw / "alerts" / "radar.jsonl").read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("state") == "firing":
            rows.append({**r, "at": _t(r["ts"])})
    return rows


def test_with_no_incident_in_the_window_the_symptom_is_its_first_most_severe_alert(
        preprocessed_control):
    from e2ekg.scenario.variants import variant_scenarios
    window = next(s for s in variant_scenarios() if s.name == "v-control").window
    firings = [r for r in _raw_firings(preprocessed_control.raw)
               if window.start_utc <= r["at"] < window.end_utc]
    assert {r["severity"] for r in firings} == {"warning"}      # no live fault, no critical alert
    first = min(firings, key=lambda r: r["at"])
    c = live_cutoff(preprocessed_control.proc)
    assert c.symptom == Symptom("alert", first["alert_id"])
    assert (c.alert_at, c.as_of) == (first["at"], first["at"] + AS_OF_AFTER_ALERT)
    incidents = pl.read_parquet(preprocessed_control.proc / "incidents.parquet")
    assert incidents.height and (incidents["ts_utc"] < window.start_utc.astimezone(UTC)).all()


def test_every_enricher_succeeds_and_the_as_of_store_is_shacl_clean(harvested_hero_as_of):
    r = harvested_hero_as_of.report
    assert all(v.success for v in r.results.values()), r.results
    assert r.shacl_ok, r.shacl_text[:3000]
    report = json.loads((harvested_hero_as_of.out / "harvest.json").read_text())
    assert _t(report["as_of"]) == harvested_hero_as_of.as_of


def test_the_live_incident_is_similar_to_the_api_gateway_deploys(harvested_hero_as_of):
    st = harvested_hero_as_of.store
    sim = {r["id"] for r in q(st, "SELECT ?id WHERE { ?l inc:incidentId 'INC-5377' ; "
                                  "ops:similarTo ?j . ?j inc:incidentId ?id }")}
    deploys = {r["id"] for r in q(st, f"SELECT ?id WHERE {{ ?j inc:incidentId ?id ; "
                                      f"ops:causeKind/skos:notation 'deployment' ; "
                                      f"ops:causeApp <{API_GATEWAY}> }}")}
    assert len(deploys) == 10 and sim == deploys
    assert "INC-5287" not in sim


def test_the_live_incident_is_its_version_at_the_cutoff(harvested_hero_as_of):
    st = harvested_hero_as_of.store
    assert q(st, "SELECT ?a WHERE { ?l inc:incidentId 'INC-5377' ; ops:affects ?a }") == [
        {"a": API_GATEWAY}]
    assert not q(st, "ASK { ?l inc:incidentId 'INC-5377' ; "
                     "inc:previouslyAffected|inc:correctedAt ?x }")


def test_no_fact_in_the_as_of_store_is_after_the_cutoff(harvested_hero_as_of):
    st, as_of = harvested_hero_as_of.store, harvested_hero_as_of.as_of
    times = q(st, "SELECT DISTINCT ?o WHERE { ?s ?p ?o FILTER(datatype(?o) = xsd:dateTime) }")
    assert times and max(_t(r["o"]) for r in times) <= as_of
    ids, sels = raw_selectors(st)
    windows = [json.loads(s) for s in sels]
    assert all(w["t_start"] != w["t_end"] for w in windows)          # no untimed rows
    assert max(_t(w["t_end"]) for w in windows) <= as_of
    proc = harvested_hero_as_of.proc
    seen = set()
    for t in DIRECT_TABLES:
        seen |= set(scan(proc, t, as_of).select("raw_id").collect()["raw_id"])
    assert seen and ids <= seen
    later = pl.scan_parquet(proc / "slack.parquet").filter(pl.col("ts_utc") >= as_of)
    assert not ids & set(later.select("raw_id").collect()["raw_id"])


def test_a_trace_fingerprint_spanning_the_cutoff_has_its_clipped_aggregates(
        harvested_hero, harvested_hero_as_of):
    as_of = harvested_hero_as_of.as_of
    body = ("SELECT ?f ?t0 ?t1 ?n ?p50 ?p99 ?rate ?sel WHERE { ?f a ops:TraceEdgeFingerprint ; "
            "ops:windowStart ?t0 ; ops:windowEnd ?t1 ; ops:count ?n ; trc:p50Ms ?p50 ; "
            "trc:p99Ms ?p99 ; trc:errorRate ?rate ; ops:rawSelector ?sel }")
    full = {r["f"]: r for r in q(harvested_hero.store, body)}
    clipped = {r["f"]: r for r in q(harvested_hero_as_of.store, body)}
    spanning = [f for f, r in full.items() if _t(r["t0"]) < as_of < _t(r["t1"])]
    assert spanning and set(spanning) <= set(clipped)
    edges = pl.read_parquet(harvested_hero.proc / "derived" / "fp_edges.parquet")
    for f in spanning:
        c = clipped[f]
        assert _t(c["t1"]) <= as_of and int(c["n"]) < int(full[f]["n"])
        key = json.loads(c["sel"])["key"]
        rows = edges.filter((pl.col("key") == key) & (pl.col("minute") >= _t(c["t0"]))
                            & (pl.col("minute") < _t(c["t1"])))
        assert int(c["n"]) == rows["n"].sum()
        assert float(c["p50"]) == pytest.approx(rows["p50_ms"].max(), abs=1e-3)
        assert float(c["p99"]) == pytest.approx(rows["p99_ms"].max(), abs=1e-3)
        rate = (rows["error_rate"] * rows["n"]).sum() / rows["n"].sum()
        assert float(c["rate"]) == pytest.approx(rate, abs=1e-6)


def test_a_table_without_an_as_of_rule_is_refused(preprocessed_hero):
    with pytest.raises(KeyError, match="no as-of rule"):
        scan(preprocessed_hero.proc, "quarantine", live_cutoff(preprocessed_hero.proc).as_of)


def test_cli_harvest_as_of(tmp_path, preprocessed_hero):
    from e2ekg.cli import main
    assert main(["harvest", "--scenario", "hero", "--proc", str(preprocessed_hero.proc.parent),
                 "--out", str(tmp_path), "--llm", "scripted", "--as-of", "--no-shacl"]) == 0
    report = json.loads((tmp_path / "hero" / "as_of" / "harvest.json").read_text())
    assert _t(report["as_of"]) == live_cutoff(preprocessed_hero.proc).as_of
    assert not (tmp_path / "hero" / "store").exists()
