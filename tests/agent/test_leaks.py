"""Nothing stamped after the cutoff reaches the agent (R21, R34). Each test below fails if its
guard is removed: the matching call on the FULL store, or over the whole raw window, returns
post-cutoff content."""
import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from conftest import call

from e2ekg.agent.tools import _observable
from e2ekg.graph.uris import change_uri
from e2ekg.preprocess import selector as sel

STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z")
LATE = {"start": "2026-10-14T22:30Z", "end": "2026-10-15T12:00Z"}
INC = "https://kg.local/inc/incident/hero/INC-5377"


def stamps(text: str) -> list[datetime]:
    return [datetime.fromisoformat((t[:-1] + (":00" if len(t) == 17 else "")) + "+00:00")
            for t in STAMP.findall(text)]


def battery(tools, symptom):
    """Every graph tool, asked for the hours around and after the cutoff."""
    return [("find_symptom", {"alert_id": "radar-09000"}),
            ("changes_in_window", {"apps": [], **LATE}),
            ("anomalies_in_window", {"apps": [], **LATE}),
            ("log_spikes", {"apps": [], **LATE}), ("trace_edges", {"apps": [], **LATE}),
            ("alerts_incidents", LATE),
            ("similar_incidents", {"app": "api-gateway", "kinds": []}),
            ("chat_claims", {"channel": "inc-5377-homepage-ttr", "kinds": []}),
            ("chat_claims", {"channel": "inc-5377-homepage-ttr",
                             "kinds": ["root_cause", "mitigation"]}),
            ("causal_chains", {"symptom_uri": symptom})]


def outputs(tools, symptom) -> list[tuple[str, str]]:
    """Each tool's result, less find_symptom's own statement of the cutoff."""
    return [(n, re.sub(r',"as_of":"[^"]*"', "", tools[n].invoke(a)))
            for n, a in battery(tools, symptom)]


def test_no_graph_tool_returns_a_time_at_or_after_the_cutoff(live):
    for name, out in outputs(live.tools, live.symptom):
        late = [t for t in stamps(out) if t >= live.as_of]
        assert not late, (name, late[:3])


def test_the_same_calls_on_the_full_store_do_cross_the_cutoff(live, full):
    crossing = {n for n, out in outputs(full, live.symptom)
                if any(t >= live.as_of for t in stamps(out))}
    assert {"alerts_incidents", "chat_claims", "changes_in_window"} <= crossing


def test_chat_names_no_cause_before_the_cutoff(live, full):
    ask = {"channel": "inc-5377-homepage-ttr", "kinds": ["root_cause"]}
    assert call(live.tools, "chat_claims", **ask) == []
    assert len(call(full, "chat_claims", **ask)) >= 3


def test_sparql_select_finds_no_datetime_after_the_cutoff(live, full):
    q = ('SELECT ?s ?p ?t WHERE { ?s ?p ?t FILTER(datatype(?t) = xsd:dateTime && '
         '?t > "2026-10-14T23:00:00Z"^^xsd:dateTime) }')
    assert call(live.tools, "sparql_select", query=q) == []
    at = ('SELECT ?s WHERE { ?s ?p ?t FILTER(?p IN (ops:atTime, ops:windowStart) && '
          '?t >= "2026-10-14T23:00:00Z"^^xsd:dateTime) }')
    assert call(live.tools, "sparql_select", query=at) == []
    assert call(full, "sparql_select", query=at)


def test_sparql_select_cannot_see_the_correction_made_after_the_cutoff(live, full):
    q = (f"SELECT ?p ?o WHERE {{ <{INC}> ?p ?o FILTER(?p IN (ops:affects, inc:correctedAt, "
         "inc:previouslyAffected)) }")
    seen = {(r["p"].rsplit("#", 1)[1], r["o"].rsplit("/", 1)[-1])
            for r in call(live.tools, "sparql_select", query=q)}
    assert seen == {("affects", "api-gateway")}
    corrected = {r["p"].rsplit("#", 1)[1] for r in call(full, "sparql_select", query=q)}
    assert {"correctedAt", "previouslyAffected"} <= corrected


def test_sparql_select_runs_on_the_store_it_was_given_and_nothing_else(live):
    q = ('SELECT * WHERE { SERVICE <file:///dev/null> { ?s ?p ?o } }')
    assert live.tools["sparql_select"].invoke({"query": q}).startswith("error")
    graphs = call(live.tools, "sparql_select", query="SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }")
    assert all("/full" not in g["g"] for g in graphs)


def test_causal_chains_end_at_the_incident_as_it_was_known_at_the_cutoff(live, full):
    last = call(live.tools, "causal_chains", symptom_uri=live.symptom)[0]["links"][-1]
    assert last[:2] == ["incident", "api-gateway"]
    corrected = call(full, "causal_chains", symptom_uri=live.symptom)
    assert all(c["links"][-1][1] != "api-gateway" for c in corrected)


def test_similar_incidents_are_those_knowable_at_the_cutoff(live, full):
    asked = {"app": "api-gateway", "kinds": ["deployment"]}
    now = call(live.tools, "similar_incidents", **asked)["incidents"]
    assert len(now) == 10 and all(i["like_live"] for i in now)
    assert "INC-5287" not in {i["id"] for i in now}
    later = call(full, "similar_incidents", **asked)["incidents"]
    assert {i["id"] for i in later} == {i["id"] for i in now}
    assert not any(i["like_live"] for i in later)
    twin = {"app": "TMS", "kinds": ["config_change"]}
    assert not any(i["like_live"] for i in call(live.tools, "similar_incidents", **twin)["incidents"])
    assert {i["id"]: i["like_live"] for i in call(full, "similar_incidents", **twin)["incidents"]
            }["INC-5287"]


# --- raw_evidence: the cutoff is its own ---------------------------------------------------

def _logs(live) -> pl.DataFrame:
    return pl.read_parquet(live.proc / "logs.parquet", columns=["raw_id", "ts_utc", "app_uri"])


def _evidence(tools, request, limit=100) -> list[dict]:
    out = tools["raw_evidence"].invoke({"uri_or_selector": request if isinstance(request, str)
                                        else json.dumps(request), "limit": limit})
    rows = json.loads(out)
    assert isinstance(rows, list), out
    return rows


def _times(live, ids: list[str]) -> list[datetime]:
    return _logs(live).filter(pl.col("raw_id").is_in(ids))["ts_utc"].to_list()


def _busy_app(live) -> str:
    cut = live.as_of
    before = _logs(live).filter(pl.col("ts_utc").is_between(cut - timedelta(minutes=1), cut,
                                                            closed="left"))
    return before.group_by("app_uri").len().sort("len", descending=True)["app_uri"][0].rsplit("/", 1)[1]


def test_a_selector_that_runs_past_the_cutoff_returns_only_what_came_before_it(live):
    app, cut = _busy_app(live), live.as_of
    logs = _logs(live).filter(pl.col("app_uri").str.ends_with("/" + app))
    start = logs.filter(pl.col("ts_utc") < cut).sort("ts_utc").tail(3)["ts_utc"][0]
    last_seconds = logs.filter(pl.col("ts_utc").is_between(start, cut, closed="left"))
    after = logs.filter(pl.col("ts_utc") >= cut)
    assert after.height > 0 and last_seconds.height > 0
    rows = _evidence(live.tools, {"source": "logs", "app": app, "t_start": start.isoformat(),
                                  "t_end": "2099-01-01T00:00Z"})
    assert sorted(r["raw_id"] for r in rows) == sorted(last_seconds["raw_id"].to_list())
    assert not set(r["raw_id"] for r in rows) & set(after["raw_id"].to_list())


def test_a_selector_that_starts_at_the_cutoff_returns_nothing(live):
    app = _busy_app(live)
    assert _evidence(live.tools, {"source": "logs", "app": app,
                                  "t_start": _z(live.as_of), "t_end": "2099-01-01T00:00Z"}) == []


@pytest.mark.parametrize("window", [
    {}, {"t_end": "2099-01-01T00:00Z"}, {"t_start": "2030-01-01T00:00Z"},
    {"t_start": "2026-10-14T23:00Z", "t_end": "2026-10-14T23:00Z"}])
def test_every_way_of_writing_a_selector_stays_before_the_cutoff(live, window):
    rows = _evidence(live.tools, {"source": "logs", "app": _busy_app(live), **window})
    assert all(t < live.as_of for t in _times(live, [r["raw_id"] for r in rows]))


def test_a_raw_id_stamped_after_the_cutoff_is_not_returned(live):
    logs = _logs(live)
    late = logs.filter(pl.col("ts_utc") >= live.as_of)["raw_id"][0]
    early = logs.filter(pl.col("ts_utc") < live.as_of)["raw_id"][0]
    assert _evidence(live.tools, late) == []
    assert [r["raw_id"] for r in _evidence(live.tools, early)] == [early]


def test_an_event_record_stamped_after_the_cutoff_is_not_returned(live):
    late = pl.read_parquet(live.proc / "alerts.parquet").filter(
        pl.col("ts_utc") >= live.as_of)["raw_id"]
    assert late.len() > 0
    assert _evidence(live.tools, late[0]) == []


def test_a_node_the_as_of_graph_does_not_hold_is_refused(live, full):
    q = ('SELECT ?s WHERE { ?s a ops:Alert ; ops:atTime ?t FILTER(?t >= '
         '"2026-10-14T23:00:00Z"^^xsd:dateTime) } LIMIT 1')
    (late,) = call(full, "sparql_select", query=q)
    out = live.tools["raw_evidence"].invoke({"uri_or_selector": late["s"]})
    assert out.startswith("error") and "not in the graph" in out


def _selected(live, full, node: str) -> pl.DataFrame:
    (row,) = call(full, "sparql_select",
                  query=f"SELECT ?o WHERE {{ <{node}> ops:rawSelector ?o }}")
    return sel.select(live.proc, sel.Selector.from_json(row["o"])).with_columns(
        pl.col("ts_utc").dt.convert_time_zone("UTC"))


def test_a_spike_after_the_cutoff_yields_no_lines_even_over_the_full_store(live, full):
    q = ('SELECT ?s WHERE { ?s a ops:LogSpike ; ops:atTime ?t FILTER(?t >= '
         '"2026-10-14T23:00:00Z"^^xsd:dateTime) } ORDER BY ?t LIMIT 1')
    (late,) = call(full, "sparql_select", query=q)
    assert _selected(live, full, late["s"]).height > 0
    assert call(full, "raw_evidence", uri_or_selector=late["s"], limit=100) == []


def test_a_fingerprint_that_runs_past_the_cutoff_yields_only_lines_before_it(live, full):
    q = ('SELECT ?s WHERE { ?s a ops:TraceEdgeFingerprint ; ops:atTime ?t ; ops:windowEnd ?e '
         'FILTER(?t < "2026-10-14T23:00:00Z"^^xsd:dateTime && '
         '?e > "2026-10-14T23:00:00Z"^^xsd:dateTime) } LIMIT 1')
    (spanning,) = call(full, "sparql_select", query=q)
    assert _selected(live, full, spanning["s"]).filter(pl.col("ts_utc") >= live.as_of).height > 0
    rows = call(full, "raw_evidence", uri_or_selector=spanning["s"], limit=100)
    assert rows
    assert all(datetime.fromisoformat(r["ts"].replace("Z", "+00:00")) < live.as_of for r in rows)


def test_a_record_with_any_row_after_the_cutoff_is_not_observable(tmp_path):
    cut = datetime(2026, 10, 14, 23, 0, tzinfo=UTC)
    rows = [("logs|a|0|1|aaaaaaaaaaaaaaaa", cut - timedelta(seconds=2)),
            ("logs|b|0|1|bbbbbbbbbbbbbbbb", cut - timedelta(seconds=2)),
            ("logs|b|0|1|bbbbbbbbbbbbbbbb", cut + timedelta(seconds=2)),
            ("logs|c|0|1|cccccccccccccccc", cut - timedelta(seconds=2)),
            ("logs|c|0|1|cccccccccccccccc", None),
            ("logs|d|0|1|dddddddddddddddd", cut)]
    pl.DataFrame(rows, schema={"raw_id": pl.String, "ts_utc": pl.Datetime("us", "UTC")},
                 orient="row").write_parquet(tmp_path / "logs.parquet")
    env = SimpleNamespace(proc=tmp_path, as_of=cut)
    seen = _observable(env, sorted({r for r, _ in rows}))
    assert set(seen) == {"logs|a|0|1|aaaaaaaaaaaaaaaa"}


def test_a_chain_names_only_changes_before_the_cutoff(live):
    chains = call(live.tools, "causal_chains", symptom_uri=live.symptom)
    assert all(datetime.fromisoformat(l[2].replace("Z", "+00:00")) < live.as_of
               for c in chains for l in c["links"])
    root = chains[0]["links"][0][3]
    assert root == str(change_uri("hero", "config_change", root.rsplit("/", 1)[1]))


def _z(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")
