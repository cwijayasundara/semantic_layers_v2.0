"""A hand-built reference ABox of the hero scenario, written to the ABox
conventions in ontology/core.ttl. It is what a perfect harvest would emit for
the facts the competency questions touch, so it proves each CQ's SPARQL
against its expected rows before Task 12 builds the real store. Chat facts
follow the hero channel's story (DESIGN.md section 2), plus distractors."""
from datetime import timedelta
from decimal import Decimal

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import PROV, RDF, RDFS, XSD

from e2ekg.graph.uris import (
    NS, app_uri, change_uri, concept, person_uri, region_uri, scoped_uri, team_uri, uri,
)
from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.history import TWIN_RESOLVER
from e2ekg.scenario.propagation import propagate
from e2ekg.timeutil import pt

OPS, CHAT, INC, TEAM, CHG, MET = NS["ops"], NS["chat"], NS["inc"], NS["team"], NS["chg"], NS["met"]
_CLASS = {"deployment": OPS.Deployment, "config_change": OPS.ConfigChange,
          "scaling": OPS.ScalingEvent, "experiment_allocation": OPS.ExperimentAllocation,
          "commit": OPS.Commit, "client_release": OPS.ClientRelease}
_LOG_CAT = {"pool_exhaustion": "pool_exhaustion", "timeouts": "timeout",
            "partial_response": "partial_response", "latency": "server_error"}


def _ts(dt) -> Literal:
    return Literal(dt.isoformat().replace("+00:00", "Z"), datatype=XSD.dateTime)


class _B:
    def __init__(self):
        self.g = Graph()
        self.n = 0

    def node(self, ns: str, kind: str, cls, **props) -> URIRef:
        self.n += 1
        s = uri(ns, kind, f"ref/{self.n:05d}")
        self.add(s, cls, **props)
        return s

    def add(self, s, cls, raw: bool = True, **props):
        for c in (cls if isinstance(cls, tuple) else (cls,)):
            self.g.add((s, RDF.type, c))
        if raw:
            self.g.add((s, OPS.rawSelector, Literal(f"ref|{s}|0|1|0000")))
        for k, v in props.items():
            ns, _, local = k.partition("__")
            p = NS[ns][local]
            for x in (v if isinstance(v, list) else [v]):
                self.g.add((s, p, x))


def build_hero_reference() -> Graph:
    s = hero_scenario()
    b = _B()
    g = b.g
    apps = {a.id: a for a in s.catalog}
    for a in s.catalog:
        t = team_uri(a.owner_team)
        g.add((t, RDF.type, OPS.Team))
        g.add((t, RDFS.label, Literal(a.owner_team)))
        b.add(app_uri(a.id), OPS.Application, raw=True, ops__inCatalog=Literal(True),
              ops__ownedBy=t, ops__appKind=Literal(a.kind), ops__tier=Literal(a.tier),
              ops__deployedIn=[region_uri(r) for r in a.regions])
        g.add((app_uri(a.id), RDFS.label, Literal(a.id)))
        for r in a.regions:
            g.add((region_uri(r), RDF.type, OPS.Region))
            g.add((region_uri(r), RDFS.label, Literal(r)))
    for e in s.edges:
        g.add((app_uri(e.caller), OPS.dependsOn, app_uri(e.callee)))
    for c in s.changes:
        b.add(change_uri(s.name, c.kind, c.id), _CLASS[c.kind],
              ops__changeKind=concept("change_kind", c.kind), ops__changedApp=app_uri(c.app),
              ops__atTime=_ts(c.at_utc), chg__changeId=Literal(c.id))

    def person(h):
        p = person_uri(h)
        g.add((p, RDF.type, OPS.Person))
        g.add((p, TEAM.handle, Literal(h)))
        return p

    # History incidents.
    hist = {}
    for h in s.history:
        i = scoped_uri("inc", "incident", s.name, h.id)
        hist[h.id] = i
        props = dict(ops__affects=app_uri(h.affected), ops__severity=concept("severity", h.severity),
                     ops__atTime=_ts(h.opened_utc), ops__resolvedBy=person(h.resolver_handle))
        if h.cause_change_kind:
            props["ops__causeKind"] = concept("change_kind", h.cause_change_kind)
        if h.cause_app:
            props["ops__causeApp"] = app_uri(h.cause_app)
        b.add(i, OPS.Incident, **props)
    twin = next(h for h in s.history if h.resolver_handle == TWIN_RESOLVER)

    # The live incident: first guess api-gateway, corrected 40 min later.
    by_sig = {}
    for link in s.answer_key.causal_chain:
        by_sig.setdefault(link.signal, link)
    inc_link, alert_link = by_sig["incident"], by_sig["alert"]
    live = scoped_uri("inc", "incident", s.name, "INC-5377")
    b.add(live, OPS.Incident, ops__affects=app_uri(inc_link.app),
          inc__previouslyAffected=app_uri("api-gateway"), ops__severity=concept("severity", "SEV2"),
          ops__atTime=_ts(inc_link.at_utc), inc__correctedAt=_ts(inc_link.at_utc + timedelta(minutes=40)),
          ops__similarTo=hist[twin.id])
    g.add((hist[twin.id], OPS.similarTo, live))
    # A similar incident outside last quarter must not show up in CQ04.
    other = s.history[-1]
    g.add((live, OPS.similarTo, hist[other.id]))

    # Signals from propagation.
    deg = propagate(s.fault, s.edges, s.window)
    lead = person("v.rao")
    for d in deg:
        a = apps[d.app]
        if a.kind == "client":
            series = b.node("met", "series", OPS.MetricSeries, ops__onApplication=app_uri(d.app),
                            met__metricName=Literal("client.ttr.p90"))
            b.node("met", "anomaly", OPS.Anomaly, ops__onSeries=series,
                   ops__onApplication=app_uri(d.app), ops__atTime=_ts(d.start_utc),
                   ops__magnitude=Literal(Decimal(str(d.magnitude))))
        elif d.kind == "load_shed":
            al = b.node("alrt", "alert", OPS.Alert, ops__onApplication=app_uri(d.app),
                        ops__atTime=_ts(d.start_utc + timedelta(minutes=2)))
            b.node("alrt", "page", OPS.Page, ops__onApplication=app_uri(d.app), ops__forAlert=al,
                   ops__assignedTo=lead, ops__atTime=_ts(d.start_utc + timedelta(minutes=3)))
        elif d.kind in _LOG_CAT:
            pat = b.node("log", "pattern", OPS.LogPattern, ops__onApplication=app_uri(d.app),
                         ops__errorCategory=concept("error_category", _LOG_CAT[d.kind]))
            b.node("log", "spike", OPS.LogSpike, ops__ofPattern=pat, ops__onApplication=app_uri(d.app),
                   ops__atTime=_ts(d.start_utc), ops__windowStart=_ts(d.start_utc),
                   ops__windowEnd=_ts(d.start_utc + timedelta(minutes=10)), ops__count=Literal(120),
                   ops__magnitude=Literal(Decimal("8.0")))
    al = b.node("alrt", "alert", OPS.Alert, ops__onApplication=app_uri(alert_link.app),
                ops__atTime=_ts(alert_link.at_utc))
    b.node("alrt", "page", OPS.Page, ops__onApplication=app_uri(alert_link.app), ops__forAlert=al,
           ops__assignedTo=lead, ops__atTime=_ts(alert_link.at_utc + timedelta(seconds=30)))
    # Distractors: a chronic NPE pattern (not a spike), a pre-window timeout spike, an old alert.
    b.node("log", "pattern", OPS.LogPattern, ops__onApplication=app_uri("artwork-svc"),
           ops__errorCategory=concept("error_category", "null_pointer"))
    old = b.node("log", "pattern", OPS.LogPattern, ops__onApplication=app_uri("search-svc"),
                 ops__errorCategory=concept("error_category", "timeout"))
    b.node("log", "spike", OPS.LogSpike, ops__ofPattern=old, ops__onApplication=app_uri("search-svc"),
           ops__atTime=_ts(pt(2026, 10, 14, 9, 0)))
    b.node("alrt", "alert", OPS.Alert, ops__onApplication=app_uri("search-svc"),
           ops__atTime=_ts(pt(2026, 10, 14, 11, 0)))

    # Chat: thread, messages and claims.
    thread = scoped_uri("chat", "thread", s.name, "inc-5377-homepage-ttr")
    b.add(thread, CHAT.Thread, chat__category=concept("message_category", "incident"),
          chat__forIncident=live)

    def claim(at, kind, about, extra=(), retracts=None):
        msg = b.node("chat", "message", OPS.Message, chat__inThread=thread, ops__atTime=_ts(at),
                     chat__category=concept("message_category", "incident"),
                     chat__sentiment=concept("sentiment", "neutral"),
                     chat__isIncidentRelated=Literal(True),
                     chat__classificationStatus=Literal("classified"))
        cls = {"mitigation": (OPS.Claim, OPS.Mitigation),
               "hypothesis": (OPS.Claim, OPS.Hypothesis)}.get(kind, OPS.Claim)
        props = dict(chat__claimKind=concept("claim_kind", kind), ops__atTime=_ts(at))
        if about:
            props["chat__about"] = [app_uri(x) for x in about] + list(extra)
        if retracts is not None:
            props["chat__retracts"] = retracts
        c = b.node("chat", "claim", cls, raw=False, **props)
        g.add((c, PROV.wasDerivedFrom, msg))
        return c

    exp = change_uri(s.name, "experiment_allocation", "chg-exp-0001")
    h_gw = claim(pt(2026, 10, 14, 15, 36, 30), "hypothesis", ["api-gateway"])
    h_and = claim(pt(2026, 10, 14, 15, 38, 30), "hypothesis", ["client-android"])
    h_cell = claim(pt(2026, 10, 14, 15, 39, 10), "hypothesis", ["row-ranker"], extra=[exp])
    claim(pt(2026, 10, 14, 15, 42, 0), "retraction", ["client-android"], retracts=h_and)
    claim(pt(2026, 10, 14, 15, 46, 30), "retraction", ["row-ranker"], retracts=h_cell)
    claim(pt(2026, 10, 14, 15, 56, 30), "retraction", ["api-gateway"], retracts=h_gw)
    claim(pt(2026, 10, 14, 15, 52, 20), "hypothesis", ["title-metadata-svc"])          # never retracted
    claim(pt(2026, 10, 14, 16, 0, 30), "mitigation", ["title-metadata-svc"])
    claim(pt(2026, 10, 14, 16, 1, 10), "mitigation", ["title-metadata-svc"])
    claim(pt(2026, 10, 14, 16, 41, 0), "root_cause", ["title-metadata-svc"])
    claim(pt(2026, 10, 14, 15, 47, 10), "observation", ["artwork-svc"])
    # The twin's channel last quarter: a mitigation and a root cause, both before the window.
    claim(twin.opened_utc + timedelta(minutes=20), "mitigation", ["title-metadata-svc"])
    claim(twin.opened_utc + timedelta(minutes=10), "root_cause", ["title-metadata-svc"])

    # History pattern (the misleading prior).
    sev2 = [h for h in s.history if h.severity == "SEV2"]
    gw = [h for h in sev2 if h.cause_app == "api-gateway" and h.cause_change_kind == "deployment"]
    b.node("ops", "pattern", OPS.Pattern, ops__onApplication=app_uri("api-gateway"),
           ops__causeKind=concept("change_kind", "deployment"),
           ops__severity=concept("severity", "SEV2"), ops__support=Literal(len(gw)),
           ops__frequency=Literal(Decimal(len(gw)) / Decimal(len(sev2))))

    # Causal chains: the answer-key chain and a weaker distractor.
    def chain(links, conf):
        ch = b.node("ops", "chain", OPS.CausalChain, raw=False, ops__explains=live,
                    ops__confidence=Literal(Decimal(conf)))
        g.add((ch, PROV.wasDerivedFrom, live))
        for k, (sig, app, at) in enumerate(links):
            ln = b.node("ops", "link", OPS.CausalLink, raw=False, ops__linkOrder=Literal(k),
                        ops__onApplication=app_uri(app), ops__atTime=_ts(at),
                        ops__signalType=concept("signal_type", sig), ops__cites=live)
            g.add((ch, OPS.hasLink, ln))
    chain([(x.signal, x.app, x.at_utc) for x in s.answer_key.causal_chain], "0.82")
    chain([("deployment", "api-gateway", pt(2026, 10, 14, 15, 18)),
           ("incident", "homepage-graphql", inc_link.at_utc)], "0.61")
    return g


@pytest.fixture(scope="session")
def hero_reference() -> Graph:
    return build_hero_reference()
