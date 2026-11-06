import json

import pytest
from agent_helpers import call

from e2ekg.agent.tools import CAP
from e2ekg.graph.uris import change_uri
from e2ekg.scenario.hero import hero_scenario

HERO = hero_scenario()
KIND = {c.id: c.kind for c in HERO.changes}


@pytest.fixture(scope="module")
def external(live):
    """scenario change id -> the opaque id the graph knows it by (R22)."""
    return {v: k for k, v in live.answers["external_ids"].items()}


def test_the_toolset_is_the_briefed_fourteen(tools):
    assert set(tools) == {
        "find_symptom", "dependency_closure", "changes_in_window", "anomalies_in_window",
        "log_spikes", "trace_edges", "alerts_incidents", "similar_incidents", "chat_claims",
        "causal_chains", "raw_evidence", "sparql_select", "record_finding", "read_findings"}


def test_changes_in_window_finds_root_and_herrings(tools, external):
    closure = [a["id"] for a in call(tools, "dependency_closure",
                                     app="homepage-graphql", direction="down")]
    out = call(tools, "changes_in_window",
               apps=closure, start="2026-10-14T21:40Z", end="2026-10-14T22:20Z")
    assert {external["chg-cfg-0001"], external["chg-exp-0001"]} <= {c["id"] for c in out}


def test_a_change_names_its_node_kind_app_time_and_what_changed(tools, external, live):
    out = call(tools, "changes_in_window", apps=["TMS"], start="2026-10-14T21:40Z",
               end="2026-10-14T22:20Z")
    root = next(c for c in out if c["id"] == external["chg-cfg-0001"])
    assert root["uri"] == str(change_uri("hero", "config_change", root["id"]))
    assert (root["kind"], root["app"], root["at"]) == (
        "config_change", "title-metadata-svc", "2026-10-14T21:52:00Z")
    assert "minInstances" in root["what"] and "48" in root["what"] and "12" in root["what"]


def test_raw_evidence_returns_original_bytes(tools, live):
    uri = _spike_uri(live, "title-batch-loader")
    out = call(tools, "raw_evidence", uri_or_selector=uri)
    assert out and not any("error" in r for r in out)
    assert all(_raw_bytes(live.raw, r["raw_id"]) == r["line"].encode() for r in out)


def test_raw_evidence_resolves_a_selector_the_agent_writes_itself(tools, live):
    sel = {"source": "logs", "app": "title-batch-loader",
           "t_start": "2026-10-14T22:05:00Z", "t_end": "2026-10-14T22:30:00Z"}
    out = call(tools, "raw_evidence", uri_or_selector=json.dumps(sel), limit=5)
    assert 0 < len(out) <= 5
    assert all(_raw_bytes(live.raw, r["raw_id"]) == r["line"].encode() for r in out)


def test_raw_evidence_names_an_unknown_node(tools):
    out = tools["raw_evidence"].invoke({"uri_or_selector": "https://kg.local/log/spike/hero/nope"})
    assert out.startswith("error") and "not in the graph" in out


def test_sparql_select_blocks_updates(tools):
    assert "only SELECT" in tools["sparql_select"].invoke({"query": "DELETE WHERE { ?s ?p ?o }"})
    for q in ("ASK { ?s ?p ?o }", "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
              "INSERT DATA { <a:b> <a:c> <a:d> }"):
        assert "only SELECT" in tools["sparql_select"].invoke({"query": q})


def test_sparql_select_forces_a_limit_and_knows_the_prefixes(tools):
    rows = call(tools, "sparql_select", query="SELECT ?s WHERE { ?s a ops:Application }")
    assert 0 < len(rows) <= 200
    clamped = call(tools, "sparql_select",
                   query="SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 100000")
    assert len(clamped) <= 200


def test_sparql_select_refuses_federation(tools):
    q = "SELECT * WHERE { SERVICE <http://localhost:1/sparql> { ?s ?p ?o } }"
    assert tools["sparql_select"].invoke({"query": q}).startswith("error")


def test_sparql_select_reports_a_syntax_error(tools):
    assert tools["sparql_select"].invoke({"query": "SELECT ?s WHERE {"}).startswith("error")


def test_record_finding_rejects_without_evidence(tools):
    out = tools["record_finding"].invoke({"f": {
        "about_app": "row-ranker", "claim": "x", "signal_uris": [], "confidence": 0.5}})
    assert "ops:cites" in out
    assert call(tools, "read_findings", app="row-ranker") == []


def test_nickname_input_resolves(tools):
    assert call(tools, "dependency_closure", app="TMS", direction="up")


def test_an_unknown_app_is_an_error_naming_what_was_not_understood(tools):
    out = tools["dependency_closure"].invoke({"app": "no-such-thing-9", "direction": "up"})
    assert out.startswith("error") and "no-such-thing-9" in out


def test_dependency_closure_counts_hops_from_the_app(tools):
    down = call(tools, "dependency_closure", app="homepage-graphql", direction="down")
    first = down[0]
    assert (first["id"], first["hops"]) == ("homepage-graphql", 0)
    assert {a["id"]: a["hops"] for a in down}["title-metadata-svc"] > 1
    up = {a["id"] for a in call(tools, "dependency_closure", app="title-metadata-svc",
                                direction="up")}
    assert {"title-batch-loader", "homepage-graphql", "edge-gateway"} <= up


def test_the_control_has_an_alert_to_explain_and_no_chain(harvested, preprocessed, scripted_llm):
    from agent_helpers import copy_store

    from e2ekg.agent.tools import make_tools
    h, pp = harvested("v-control", as_of=True), preprocessed("v-control")
    ctl = make_tools(copy_store(h.store), pp.proc, "v-control", "tester", llm=scripted_llm)
    out = call(ctl, "find_symptom", alert_id=h.symptom.rsplit("/", 1)[1])
    assert out["incident"] is None and out["symptom_uri"] == h.symptom
    assert call(ctl, "causal_chains", symptom_uri=h.symptom) == []


def _record(tools, app, uris, conf=0.8, claim="pool waits rise after the scale-in"):
    return tools["record_finding"].invoke({"f": {
        "about_app": app, "claim": claim, "signal_uris": uris, "confidence": conf}})


def test_a_finding_is_validated_then_written_to_its_specialists_graph(live, tools):
    evidence = _spike_uri(live, "title-batch-loader")
    out = json.loads(_record(tools, "TBL", [evidence]))
    graph = "https://kg.local/graph/findings/hero/tester"
    assert out["graph"] == graph
    assert live.store.query(f"ASK {{ GRAPH <{graph}> {{ <{out['uri']}> a "
                            "<https://kg.local/ops#Finding> } }")
    again = json.loads(_record(tools, "TBL", [evidence]))
    assert again["uri"] == out["uri"]
    found = call(tools, "read_findings", app="title-batch-loader")
    assert [f["uri"] for f in found] == [out["uri"]]
    assert found[0]["cites"] == [evidence] and found[0]["specialist"] == "tester"
    assert found[0]["app"] == "title-batch-loader" and found[0]["conf"] == 0.8
    assert call(tools, "read_findings", app="row-ranker") == []
    assert [f["uri"] for f in call(tools, "read_findings")] == [out["uri"]]


def test_a_finding_that_fails_shacl_is_not_committed(live, tools):
    evidence = _spike_uri(live, "title-batch-loader")
    out = _record(tools, "title-batch-loader", [evidence], conf=1.5, claim="over-confident")
    assert "ops:confidence" in out and not out.startswith("{")
    assert all(f["claim"] != "over-confident" for f in call(tools, "read_findings"))


def test_a_finding_cannot_cite_a_node_the_graph_does_not_hold(tools):
    out = _record(tools, "row-ranker", ["https://kg.local/met/anomaly/hero/not-here"])
    assert out.startswith("error") and "not-here" in out


def test_a_finding_about_an_unknown_app_is_an_error(live, tools):
    out = _record(tools, "no-such-thing-9", [_spike_uri(live, "title-batch-loader")])
    assert out.startswith("error") and "no-such-thing-9" in out


def test_find_symptom_names_the_alert_and_the_incident_it_opened(tools):
    out = call(tools, "find_symptom", alert_id="radar-09000")
    assert out["symptom_uri"] == out["incident"]["uri"]
    assert out["alert"]["app"] == "homepage-graphql" and out["alert"]["at"] == "2026-10-14T22:30:00Z"
    assert "TTR" in out["alert"]["summary"]
    assert out["incident"]["id"] == "INC-5377" and out["incident"]["affects"] == ["api-gateway"]
    assert out["as_of"] == "2026-10-14T23:00:00Z"


def test_find_symptom_without_an_incident_falls_back_to_the_alert(tools):
    out = call(tools, "find_symptom", alert_id="radar-09100")
    assert out["incident"] is None and out["symptom_uri"] == out["alert"]["uri"]


def test_find_symptom_on_an_unknown_alert_lists_the_known_ones(tools):
    out = tools["find_symptom"].invoke({"alert_id": "radar-99999"})
    assert out.startswith("error") and "radar-09000" in out


def test_telemetry_tools_report_the_cascade_with_citable_uris(tools):
    window = {"start": "2026-10-14T22:00Z", "end": "2026-10-14T22:30Z"}
    an = call(tools, "anomalies_in_window", apps=["title-batch-loader"], **window)
    assert an and all(a["uri"].startswith("https://kg.local/met/anomaly/") for a in an)
    assert {"app", "metric", "at", "mag"} <= set(an[0])
    sp = call(tools, "log_spikes", apps=["title-batch-loader"], **window)
    assert any("pool" in s["tpl"].lower() for s in sp)
    te = call(tools, "trace_edges", apps=["title-metadata-svc"], **window)
    assert te and all("title-metadata-svc" in (e["caller"], e["callee"]) for e in te)
    assert {"type", "p50", "err"} <= set(te[0])


def test_alerts_incidents_lists_both_kinds_in_time_order(tools):
    out = call(tools, "alerts_incidents", start="2026-10-14T22:00Z", end="2026-10-14T23:00Z")
    kinds = {r["kind"] for r in out}
    assert kinds == {"alert", "incident"}
    assert [r["at"] for r in out] == sorted(r["at"] for r in out)
    assert any(r.get("id") == "INC-5377" for r in out)


def test_chat_claims_of_the_live_channel_by_kind(tools):
    out = call(tools, "chat_claims", channel="inc-5377-homepage-ttr", kinds=["hypothesis"])
    assert out and {c["kind"] for c in out} == {"hypothesis"}
    assert {"uri", "app", "at", "by", "quote"} <= set(out[0])
    allk = call(tools, "chat_claims", channel="inc-5377-homepage-ttr", kinds=[])
    assert len({c["kind"] for c in allk}) > 1


def test_chat_claims_of_an_unknown_channel_is_an_error(tools):
    assert tools["chat_claims"].invoke({"channel": "no-such-channel", "kinds": []}
                                       ).startswith("error")


def test_causal_chains_rank_the_true_root_first(tools, external, live):
    out = call(tools, "causal_chains", symptom_uri=live.symptom)
    top = out[0]
    assert top["links"][0][3] == str(change_uri("hero", "config_change", external["chg-cfg-0001"]))
    assert top["conf"] >= 0.6 and top["links"][-1][0] == "incident"


def test_causal_chains_explain_an_alert_too(tools):
    alert = call(tools, "find_symptom", alert_id="radar-09000")["alert"]["uri"]
    out = call(tools, "causal_chains", symptom_uri=alert)
    assert out and out[0]["links"][-1][:2] == ["alert", "homepage-graphql"]


def test_causal_chains_of_a_node_that_is_no_symptom_is_an_error(tools, live):
    out = tools["causal_chains"].invoke({"symptom_uri": live.symptom + "-nope"})
    assert out.startswith("error")


def test_similar_incidents_are_the_api_gateway_deploys_knowable_at_the_cutoff(tools):
    out = call(tools, "similar_incidents", app="api-gateway", kinds=["deployment"])
    ids = {i["id"] for i in out["incidents"]}
    assert len(ids) == 10 and "INC-5287" not in ids
    assert all(i["like_live"] for i in out["incidents"])
    assert out["patterns"][0]["support"] == 10 and out["patterns"][0]["freq"] == 0.4


def test_similar_incidents_by_a_bad_kind_names_the_valid_ones(tools):
    out = tools["similar_incidents"].invoke({"app": "api-gateway", "kinds": ["teleport"]})
    assert out.startswith("error") and "config_change" in out


def test_no_result_is_longer_than_the_cap(tools, live):
    wide = {"start": "2026-10-14T00:00Z", "end": "2026-10-15T12:00Z"}
    calls = [("changes_in_window", {"apps": [], **wide}),
             ("anomalies_in_window", {"apps": [], **wide}),
             ("log_spikes", {"apps": [], **wide}), ("trace_edges", {"apps": [], **wide}),
             ("alerts_incidents", wide),
             ("dependency_closure", {"app": "edge-gateway", "direction": "down"}),
             ("chat_claims", {"channel": "inc-5377-homepage-ttr", "kinds": []}),
             ("sparql_select", {"query": "SELECT ?s ?p ?o WHERE { ?s ?p ?o }"}),
             ("raw_evidence", {"uri_or_selector": json.dumps(
                 {"source": "logs", "app": "edge-gateway"}), "limit": 100}),
             ("causal_chains", {"symptom_uri": live.symptom})]
    for name, args in calls:
        out = tools[name].invoke(args)
        assert len(out) <= CAP, name
        json.loads(out)


def test_an_overlong_result_says_how_much_it_left_out(tools):
    out = call(tools, "sparql_select", query="SELECT ?s ?p ?o WHERE { ?s ?p ?o }")
    assert out[-1]["truncated"] > 0


def _spike_uri(live, app: str) -> str:
    rows = live.store.query(
        "PREFIX ops: <https://kg.local/ops#> SELECT ?s WHERE { ?s a ops:LogSpike ; "
        f"ops:onApplication <https://kg.local/app/application/{app}> ; ops:atTime ?t "
        "} ORDER BY ?t LIMIT 1")
    return rows[0]["s"]


def _raw_bytes(raw_root, raw_id: str) -> bytes:
    from e2ekg.rawid import RawSpan
    s = RawSpan.parse(raw_id)
    with open(raw_root / s.rel_path, "rb") as f:
        f.seek(s.offset)
        return f.read(s.length)
