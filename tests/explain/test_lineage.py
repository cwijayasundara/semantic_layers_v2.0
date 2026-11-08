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
    out = lineage(live.tools, live.proc, uri)
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
        recs = lineage(live.tools, live.proc, u)["records"]
        assert recs
        for r in recs:
            assert r["processed"]["ts_raw"] and r["ts_utc"]


def test_unknown_uri_returns_no_records(live_tools):
    out = lineage(live_tools.tools, live_tools.proc, "https://kg.local/ops/finding/hero/nope")
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
