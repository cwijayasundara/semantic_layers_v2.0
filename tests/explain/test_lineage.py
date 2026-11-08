import json
from types import SimpleNamespace

from e2ekg.explain.lineage import lineage


def _uris(live, cls):
    from e2ekg.graph.uris import NS
    rows = live.store.query(f"SELECT DISTINCT ?c WHERE {{ ?c a <{NS['ops'][cls]}> }} LIMIT 5")
    return [str(r["c"]) for r in rows]


def test_config_change_lineage(live_tools):
    live = live_tools
    from e2ekg.graph.uris import NS
    rows = live.store.query(f"""SELECT ?c WHERE {{ ?c a <{NS['ops']['ConfigChange']}> ;
        <{NS['chg']['property']}> ?p FILTER(STRENDS(?p, "minInstances")) }} LIMIT 1""")
    uri = str(next(iter(rows))["c"])
    out = lineage(live.tools, live.proc, uri, as_of=live.as_of)
    r = out["records"][0]
    assert r["file"] == "events/properties.log"
    assert "minInstances" in r["line"]
    assert r["processed"]["table"] == "events"
    assert out["graph"]


def test_log_spike_lineage_shows_timestamp_normalisation(live_tools):
    live = live_tools
    uris = _uris(live, "LogSpike")
    assert uris
    for u in uris:
        recs = lineage(live.tools, live.proc, u, as_of=live.as_of)["records"]
        assert recs
        for r in recs:
            assert r["processed"]["ts_raw"] and r["ts_utc"]


def test_unknown_uri_returns_no_records(live_tools):
    out = lineage(live_tools.tools, live_tools.proc, "https://kg.local/ops/finding/hero/nope",
                  as_of=live_tools.as_of)
    assert out["records"] == []


def test_a_log_lineage_shows_the_template_mined_at_the_cutoff(live_tools):
    """The processed row's template is the as-of view's, not the full window's."""
    from helpers import asof_only_pattern

    from e2ekg.graph.enricher import node
    live = live_tools
    app, tid, template, ids = asof_only_pattern(live.proc, live.as_of)
    out = lineage(live.tools, live.proc, str(node("log", "pattern", "hero", app, tid)),
                  as_of=live.as_of)
    assert [r["raw_id"] for r in out["records"]] == ids[:3]
    assert {r["processed"]["template"] for r in out["records"]} == {template}


def test_a_uri_that_is_not_an_iri_never_reaches_sparql(live_tools):
    asked = []
    tools = {name: SimpleNamespace(invoke=lambda args, name=name: asked.append(name) or "[]")
             for name in ("raw_evidence", "sparql_select")}
    bad = "https://kg.local/x> ?p ?o } } SELECT * WHERE { GRAPH ?g { <https://kg.local/y"
    out = lineage(tools, live_tools.proc, bad, as_of=live_tools.as_of)
    assert out == {"uri": bad, "graph": None, "records": []}
    assert asked == []


def test_an_integrity_failure_is_kept_and_flagged(live_tools):
    live = live_tools
    uri = _uris(live, "LogSpike")[0]
    good = lineage(live.tools, live.proc, uri, as_of=live.as_of)["records"][0]
    assert good["integrity_error"] is None
    tools = {**live.tools, "raw_evidence": SimpleNamespace(invoke=lambda args: json.dumps(
        [{"raw_id": good["raw_id"], "error": "the raw bytes do not match the id"}]))}
    [rec] = lineage(tools, live.proc, uri, as_of=live.as_of)["records"]
    assert rec["integrity_error"] == "the raw bytes do not match the id"
    assert (rec["raw_id"], rec["file"], rec["line"]) == (good["raw_id"], good["file"], None)
