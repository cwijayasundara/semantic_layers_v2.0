"""The enricher framework and each enricher's conventions (core.ttl header)."""
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime

import polars as pl
import pytest
from rdflib.namespace import RDF

from e2ekg.graph.enricher import Enricher, EnrichmentResult, recovered
from e2ekg.graph.enrichers import ENRICHERS
from e2ekg.graph.enrichers.alerts_incidents import severity
from e2ekg.graph.enrichers.logs import access_status, error_category
from e2ekg.graph.enrichers.topology import merge_edges
from e2ekg.graph.shacl import ENRICHER_NAMES, LATER_STAGES
from e2ekg.graph.store import Store, graph_uri
from e2ekg.graph.uris import NS, app_uri
from e2ekg.preprocess.selector import Selector, select

OPS = NS["ops"]
PFX = """PREFIX ops: <https://kg.local/ops#>
PREFIX chg: <https://kg.local/chg#>
PREFIX chat: <https://kg.local/chat#>
PREFIX inc: <https://kg.local/inc#>
PREFIX trc: <https://kg.local/trc#>
PREFIX log: <https://kg.local/log#>
PREFIX met: <https://kg.local/met#>
PREFIX team: <https://kg.local/team#>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""


def q(store, body: str) -> list[dict]:
    return store.query(PFX + body)


def _app(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


# --- the framework ---------------------------------------------------------------

def test_enrichers_in_the_brief_order_named_as_the_cq_requires():
    names = [e.name for e in ENRICHERS]
    assert names == ["catalog", "topology", "changes", "metrics", "logs", "traces",
                     "alerts_incidents", "chat", "history"]
    assert [n for n in ENRICHER_NAMES if n not in LATER_STAGES] == names
    assert [e.__name__ for e in ENRICHERS] == [
        "CatalogEnricher", "TopologyEnricher", "ChangesEnricher", "MetricAnomalyEnricher",
        "LogPatternEnricher", "TraceFingerprintEnricher", "AlertsIncidentsEnricher",
        "ChatEnricher", "HistoryEnricher"]
    for e in ENRICHERS:
        assert e.reads, e.name


class _Ctx:
    def __init__(self):
        self.scenario, self.store = "hero", Store(None)


class _Toy(Enricher):
    name, reads = "toy", ("nothing",)

    def fetch(self, ctx):
        return 3

    def analyze(self, raw):
        return range(raw)

    def emit(self, result):
        for i in result:
            yield app_uri(f"a{i}"), RDF.type, OPS.Application


def test_run_replaces_its_named_graph_and_is_idempotent():
    ctx = _Ctx()
    r1, r2 = _Toy().run(ctx), _Toy().run(ctx)
    assert r1 == r2 == EnrichmentResult(success=True, triples_added=3, error=None)
    assert ctx.store.quad_count() == 3
    assert q(ctx.store, "SELECT (COUNT(*) AS ?n) WHERE { GRAPH <https://kg.local/graph/toy/hero>"
                        " { ?s ?p ?o } }") == [{"n": "3"}]


def test_run_is_best_effort_and_a_failure_clears_the_stale_graph():
    ctx = _Ctx()
    _Toy().run(ctx)

    class Broken(_Toy):
        def analyze(self, raw):
            raise ValueError("bad input")
    r = Broken().run(ctx)
    assert r.success is False and r.triples_added == 0 and "bad input" in r.error
    assert ctx.store.quad_count() == 0


def test_a_field_moved_into_the_description_is_read_back():
    assert recovered({"severity": "SEV2"}, "severity") == "SEV2"
    moved = {"description": "spike (severity: SEV1) (urgency: high)"}
    assert recovered(moved, "severity") == "SEV1"
    assert recovered({"description": "nothing here"}, "severity") is None


@pytest.mark.parametrize("text,sev", [("SEV2", "SEV2"), ("sev 1", "SEV1"), ("[SEV-3] x", "SEV3"),
                                      ("severe", None), (None, None)])
def test_severity_surface_forms(text, sev):
    assert severity(text) == sev


# --- pure analysis pieces -----------------------------------------------------------

def test_merge_edges_caps_at_50_per_direction_by_call_volume():
    a = lambda i: f"app-{i:03d}"          # noqa: E731
    catalog = {("hub", a(i)) for i in range(60)} | {(a(i), "sink") for i in range(55)}
    traces = {("hub", a(i)): 1000 - i for i in range(10, 60)}   # volume ranks a010.. first
    traces[("hub", "new-callee")] = 5000                          # trace-only edge
    kept, dropped = merge_edges(catalog, traces, cap=50)
    out_hub = [c for (h, c) in kept if h == "hub"]
    assert len(out_hub) == 50 and "new-callee" in out_hub
    assert "app-000" not in out_hub and "app-010" in out_hub      # no traffic loses
    assert len([1 for (_, c) in kept if c == "sink"]) == 50
    assert dropped["hub"] == 11 and dropped["sink"] == 5
    assert merge_edges(catalog, traces, cap=50) == (kept, dropped)  # deterministic


@pytest.mark.parametrize("text,level,cat", [
    ("Connection pool exhausted (active=<*>, idle=<*>, waiting=<*>) calling title-metadata-svc",
     "ERROR", "pool_exhaustion"),
    ("Deadline exceeded calling <*> after <*>ms", "ERROR", "timeout"),
    ("Thread pool saturated: rejected execution (pool=grpc-server, active=<*>, queue=<*>)",
     "WARN", "load_shed"),
    ("Partial response for query HomePage: <*> of <*> fields resolved with errors", "WARN",
     "partial_response"),
    ("java.lang.NullPointerException at <*>", "ERROR", "null_pointer"),
    ("Connection refused to <*> during instance warm-up; retrying", "WARN", "other"),
    ("Instance registered as STARTING, traffic not yet routed", "INFO", None),
    ("POST <*> 504", "ERROR", "timeout"),
    ("POST <*> 503", "ERROR", "load_shed"),
    ("GET <*> 500", "ERROR", "server_error"),
])
def test_error_category_rules(text, level, cat):
    assert error_category(text, level) == cat


def test_access_status_only_for_access_log_templates():
    assert access_status("POST <*> <*>", ["/graphql?op=HomePage", "504"]) == 504
    assert access_status("GET <*> <*>", ["/x", "abc"]) is None
    assert access_status("Deadline exceeded calling <*> after <*>ms", ["a", "504"]) is None


def test_a_series_with_untimed_rows_is_one_node_with_one_count():
    from e2ekg.graph.enrichers.metrics import MetricAnomalyEnricher
    e = MetricAnomalyEnricher()
    e.scenario = "hero"
    t = datetime(2026, 10, 14, 19, tzinfo=UTC)
    rows = [{"app_uri": None, "key": "m|a=b", "t_start": t, "t_end": t, "n": 5, "selector": "{t}"},
            {"app_uri": None, "key": "m|a=b", "t_start": None, "t_end": None, "n": 2,
             "selector": "{u}"}]
    triples = list(e.emit(e.analyze((rows, []))))
    assert [int(o) for _, p, o in triples if p == OPS["count"]] == [7]
    assert {str(o) for _, p, o in triples if p == OPS.rawSelector} == {"{t}", "{u}"}


# --- each enricher on the 0.05 hero ---------------------------------------------------

def test_every_enricher_succeeded_with_triples(harvested_hero):
    for name, r in harvested_hero.report.results.items():
        assert r.success, (name, r.error)
        assert r.triples_added > 0, name


def test_catalog_gates_applications(harvested_hero):
    st = harvested_hero.store
    rows = q(st, "SELECT (COUNT(DISTINCT ?a) AS ?n) WHERE { ?a a ops:Application ; "
                 "ops:inCatalog true ; ops:ownedBy/rdfs:label ?t ; ops:appKind ?k ; "
                 "ops:deployedIn/rdfs:label ?r ; ops:rawSelector ?raw }")
    assert rows == [{"n": "150"}]
    assert q(st, "ASK { ?x ops:onApplication|ops:changedApp|ops:affects ?a "
                 "FILTER NOT EXISTS { ?a a ops:Application } }") is False


def test_topology_merges_catalog_and_trace_edges(harvested_hero):
    st = harvested_hero.store
    n = int(q(st, "SELECT (COUNT(*) AS ?n) WHERE { ?a ops:dependsOn ?b }")[0]["n"])
    assert n > 100
    # a datastore edge (catalog only; trace edges are cross-service)
    assert q(st, "ASK { <https://kg.local/app/application/title-metadata-svc> ops:dependsOn "
                 "?d . ?d ops:appKind ?k FILTER(?k IN ('datastore', 'cache')) }")


def test_changes_follow_the_core_header_conventions(harvested_hero, preprocessed_hero):
    st = harvested_hero.store
    answers = json.loads((preprocessed_hero.answers / "hero.json").read_text())
    rows = q(st, "SELECT ?c ?id ?kind ?t ?app WHERE { ?c a/rdfs:subClassOf* ops:Change ; "
                 "chg:changeId ?id ; ops:changeKind/skos:notation ?kind . "
                 "OPTIONAL { ?c ops:atTime ?t } OPTIONAL { ?c ops:changedApp ?app } }")
    ids = Counter(r["id"] for r in rows)
    assert all(n == 1 for n in ids.values())                  # one node per external id
    assert set(answers["external_ids"]) <= set(ids)           # each scenario change, by external id
    assert not any(r["id"].startswith("chg-") for r in rows)  # no scenario ids (R22)
    kinds = Counter(r["kind"] for r in rows)
    assert kinds["scaling"] == 10 and kinds["config_change"] == 6   # every ASG / property line
    ext = {v: k for k, v in answers["external_ids"].items()}
    dep = {r["id"]: r for r in rows}[ext["chg-dep-0001"]]     # the api-gateway deploy
    assert dep["t"] == "2026-10-14T22:18:00Z"                 # SUCCEEDED, not RUNNING (22:14)
    commits = [r for r in rows if r["kind"] == "commit"]
    assert commits and all(r["app"] for r in commits)         # each from its "[app]" prefix


def test_metrics_series_and_anomalies(harvested_hero):
    st = harvested_hero.store
    rows = q(st, "SELECT DISTINCT ?app WHERE { ?x a ops:Anomaly ; ops:onSeries ?s ; "
                 "ops:onApplication ?app ; met:direction 'up' ; ops:magnitude ?m . "
                 "?s a ops:MetricSeries ; met:metricName 'client.ttr.p90' }")
    assert {_app(r["app"]) for r in rows} == {"client-android", "client-ios", "client-tv",
                                              "client-web"}


def test_logs_patterns_spikes_and_status_refinement(harvested_hero):
    st = harvested_hero.store
    rows = q(st, "SELECT DISTINCT ?app ?cat WHERE { ?s a ops:LogSpike ; ops:ofPattern ?p . "
                 "?p ops:onApplication ?app ; ops:errorCategory/skos:notation ?cat }")
    got = {(_app(r["app"]), r["cat"]) for r in rows}
    assert ("title-batch-loader", "pool_exhaustion") in got
    assert ("edge-gateway", "timeout") in got                 # the refined nginx 504 pattern
    assert q(st, "ASK { ?p log:refines ?parent ; log:httpStatus 504 ; ops:count ?n . "
                 "?parent a ops:LogPattern }")


def test_traces_edges_cover_every_span(harvested_hero, preprocessed_hero):
    import polars as pl
    st = harvested_hero.store
    n = int(q(st, "SELECT (SUM(?n) AS ?t) WHERE { ?e a trc:TraceEdge ; ops:count ?n }")[0]["t"])
    spans = pl.scan_parquet(preprocessed_hero.proc / "spans.parquet").select(pl.len())
    assert n == spans.collect().item()
    rows = q(st, "SELECT ?f (COUNT(?st) AS ?k) WHERE { ?f a ops:TraceEdgeFingerprint ; "
                 "ops:signalType ?st ; trc:onEdge ?e } GROUP BY ?f")
    assert rows and all(r["k"] == "1" for r in rows)


def test_fingerprint_counts_equal_their_selectors(harvested_hero, preprocessed_hero):
    st = harvested_hero.store
    rows = q(st, "SELECT ?x ?sel ?n WHERE { ?x a ?c ; ops:rawSelector ?sel ; ops:count ?n "
                 "VALUES ?c { ops:LogSpike ops:MetricSeries trc:TraceEdge "
                 "ops:TraceEdgeFingerprint ops:LogPattern } "
                 "FILTER NOT EXISTS { ?x log:refines ?p } } ORDER BY ?x")
    sels, count = defaultdict(list), {}
    for r in rows:
        sels[r["x"]].append(r["sel"])
        count[r["x"]] = int(r["n"])
    assert len(sels) > 100
    for x in sorted(sels)[:: max(1, len(sels) // 40)]:
        got = sum(select(preprocessed_hero.proc, Selector.from_json(s)).height for s in sels[x])
        assert got == count[x], x


def test_alerts_pages_incidents(harvested_hero):
    st = harvested_hero.store
    inc = q(st, "SELECT ?id ?sev ?prev ?cur ?t WHERE { ?i a ops:Incident ; inc:incidentId ?id ;"
                " ops:severity/skos:notation ?sev ; ops:atTime ?t ; ops:affects ?cur ; "
                "inc:previouslyAffected ?prev ; inc:correctedAt ?c }")
    assert inc == [{"id": "INC-5377", "sev": "SEV2", "prev": str(app_uri("api-gateway")),
                    "cur": str(app_uri("homepage-graphql")), "t": "2026-10-14T22:34:00Z"}]
    # severity is recovered when the record's field was renamed ("(severity: SEV2)")
    assert q(st, "SELECT ?s WHERE { ?i inc:incidentId 'INC-5308' ; ops:severity/skos:notation "
                 "?s }") == [{"s": "SEV2"}]
    pages = q(st, "SELECT ?h WHERE { ?p a ops:Page ; ops:atTime ?t ; ops:assignedTo/team:handle "
                  "?h } LIMIT 1")
    assert pages


def test_chat_messages_claims_and_unclassified(harvested_hero):
    st = harvested_hero.store
    # DISTINCT: Oxigraph's union default graph is a bag, and Person nodes sit in several graphs
    n = q(st, "SELECT (COUNT(DISTINCT ?m) AS ?n) WHERE { ?m a ops:Message ; chat:inThread ?t ; "
              "chat:classificationStatus ?s ; chat:postedBy/team:handle ?h ; "
              "ops:rawSelector ?raw ; ops:atTime ?at }")
    slack = pl.scan_parquet(harvested_hero.proc / "slack.parquet").select(pl.len()).collect()
    assert int(n[0]["n"]) == slack.item()
    assert q(st, "ASK { ?c a ops:Claim, ops:Mitigation ; chat:claimKind/skos:notation "
                 "'mitigation' ; chat:quote ?q ; prov:wasDerivedFrom ?m . ?m a ops:Message }")
    assert q(st, "ASK { ?t a chat:Thread ; chat:forIncident ?i . ?i inc:incidentId 'INC-5377' }")
    # microsecond Slack time survives
    assert q(st, "ASK { ?m a ops:Message ; ops:atTime ?t "
                 "FILTER(STR(?t) = '2026-10-14T23:00:30.986921Z') }")


def test_chat_unclassified_rows_get_chat_unclassified():
    from e2ekg.graph.enrichers.chat import message_triples
    row = {"raw_id": "chat|slack/c/messages.jsonl|0|1|00", "channel": "c", "ts": "1.000001",
           "ts_utc": datetime(2026, 1, 1, 0, 0, 1, 1, tzinfo=UTC), "user": "U1",
           "category": "unclassified", "sentiment": None, "is_incident_related": None,
           "status": "unclassified"}
    ts = set(message_triples("hero", row, handle="x.y"))
    cats = {o for _, p, o in ts if p == NS["chat"].category}
    assert cats == {NS["chat"].Unclassified}
    assert any(p == NS["chat"].classificationStatus and str(o) == "unclassified"
               for _, p, o in ts)
    assert any(p == OPS.atTime and o.value == row["ts_utc"] for _, p, o in ts)


def test_a_walk_back_retracts_only_a_hypothesis_about_the_same_app(harvested_hero):
    """"X is not the cause" withdraws a suspicion, not a measurement (R33)."""
    rows = q(harvested_hero.store, "SELECT DISTINCT ?about ?target ?isHyp WHERE { ?r chat:claimKind/"
             "skos:notation 'retraction' ; chat:about ?about ; chat:retracts ?c . "
             "?c chat:about ?target . BIND(EXISTS { ?c a ops:Hypothesis } AS ?isHyp) }")
    assert rows and all(r["about"] == r["target"] for r in rows)
    assert {str(r["isHyp"]).lower() for r in rows} == {"true"}


def test_a_causal_path_observation_is_never_retracted(harvested_hero):
    """The row-ranker walk-backs reply to observations and to the lead's chain
    "TMS slow -> ... -> the ranker deadlines"; none of the TMS evidence is withdrawn."""
    st = harvested_hero.store
    assert not q(st, "SELECT ?c WHERE { ?c chat:claimKind/skos:notation 'observation' . "
                     "?r chat:retracts ?c }")
    assert not q(st, "SELECT ?c WHERE { ?r chat:retracts ?c . ?c chat:about ?a "
                     "FILTER(?a IN (<https://kg.local/app/application/title-metadata-svc>, "
                     "<https://kg.local/app/application/title-batch-loader>)) }")
    assert q(st, "ASK { ?c chat:claimKind/skos:notation 'observation' ; chat:about "
                 "<https://kg.local/app/application/title-metadata-svc> ; chat:quote ?q "
                 "FILTER(CONTAINS(?q, 'p99') || CONTAINS(?q, 'instance')) }")


def test_threads_and_walk_back_links_cite_their_slack_records(harvested_hero):
    st = harvested_hero.store
    assert not q(st, "SELECT ?t WHERE { ?t a chat:Thread FILTER NOT EXISTS { ?t ops:rawSelector ?s } }")
    rows = q(st, "SELECT DISTINCT ?t ?sel WHERE { ?t a chat:Thread ; ops:rawSelector ?sel }")
    msg_raw = {r["m"]: r["raw"] for r in q(st, "SELECT DISTINCT ?m ?raw WHERE { ?m a ops:Message ; "
                                                "ops:rawSelector ?raw }")}
    in_thread = defaultdict(set)
    for r in q(st, "SELECT DISTINCT ?m ?t WHERE { ?m a ops:Message ; chat:inThread ?t }"):
        in_thread[r["t"]].add(msg_raw[r["m"]])
    got = defaultdict(set)
    for r in rows:
        got[r["t"]].add(r["sel"])
    assert got == in_thread
    edges = q(st, "SELECT DISTINCT ?r ?c WHERE { ?r chat:retracts ?c }")
    assert edges
    for e in edges:
        derived = {x["raw"] for x in q(st, f"SELECT ?raw WHERE {{ <{e['r']}> prov:wasDerivedFrom/"
                                           f"ops:rawSelector ?raw }}")} | {
            x["raw"] for x in q(st, f"SELECT ?raw WHERE {{ <{e['c']}> prov:wasDerivedFrom/"
                                    f"ops:rawSelector ?raw }}")}
        cited = {x["raw"] for x in q(st, f"SELECT ?raw WHERE {{ <{e['r']}> ops:rawSelector ?raw }}")}
        assert derived and derived <= cited


def test_history_patterns_and_similarity(harvested_hero):
    st = harvested_hero.store
    rows = q(st, "SELECT ?s ?f WHERE { ?p a ops:Pattern ; ops:onApplication "
                 "<https://kg.local/app/application/api-gateway> ; ops:causeKind/skos:notation "
                 "'deployment' ; ops:severity/skos:notation 'SEV2' ; ops:support ?s ; "
                 "ops:frequency ?f }")
    assert rows == [{"s": "10", "f": "0.4"}]
    sim = q(st, "SELECT ?id WHERE { ?live inc:incidentId 'INC-5377' ; ops:similarTo ?p . "
                "?p inc:incidentId ?id . ?p ops:similarTo ?live }")
    assert {r["id"] for r in sim} == {"INC-5287"}


def test_named_graph_per_enricher(harvested_hero):
    names = {r["g"] for r in q(harvested_hero.store, "SELECT DISTINCT ?g WHERE "
                                                     "{ GRAPH ?g { ?s ?p ?o } }")}
    for e in ENRICHERS:
        assert str(graph_uri(e.name, "hero")) in names
