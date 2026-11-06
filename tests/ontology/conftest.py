"""A hand-built reference ABox of the hero scenario, written to the ABox
conventions in ontology/core.ttl. It is what a perfect harvest would emit for
the facts the competency questions touch, so it proves each CQ's SPARQL
against its expected rows before Task 12 builds the real store.

Facts come from the generator (R20), through ontology/cq/_derive_expected.py:
rendered changes under their external ids, alerts, pages and incidents,
the authored hero chat beats (claims typed by their phase), and the
chat.retraction injections from the generation manifest (absent when
data/raw/_answers is missing; CQ06 then skips). Distractors outside the
window check the CQs' filters."""
import importlib.util
from datetime import timedelta
from decimal import Decimal

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib import namespace as rdfns  # not `import XSD`: pytest probes conftest attrs
from rdflib.namespace import PROV, RDF, RDFS

from e2ekg.graph.shacl import CQ_DIR
from e2ekg.graph.uris import (
    NS, app_uri, change_uri, concept, person_uri, region_uri, scoped_uri, team_uri, uri,
)
from e2ekg.timeutil import pt

OPS, CHAT, INC, TEAM, CHG, MET = NS["ops"], NS["chat"], NS["inc"], NS["team"], NS["chg"], NS["met"]
_CLASS = {"deployment": OPS.Deployment, "config_change": OPS.ConfigChange,
          "scaling": OPS.ScalingEvent, "experiment_allocation": OPS.ExperimentAllocation,
          "commit": OPS.Commit, "client_release": OPS.ClientRelease}
_LOG_CAT = {"pool_exhaustion": "pool_exhaustion", "timeouts": "timeout",
            "partial_response": "partial_response", "latency": "server_error"}
# Authored beat phase -> claim kind (verdicts and chatter carry no claim here).
_CLAIM_KIND = {"observation": "observation", "hypothesis": "hypothesis", "mitigation": "mitigation",
               "question": None, "verdict": None, "chatter": None}


def load_derive():
    spec = importlib.util.spec_from_file_location("_derive_expected", CQ_DIR / "_derive_expected.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ts(dt) -> Literal:
    return Literal(dt.isoformat().replace("+00:00", "Z"), datatype=rdfns.XSD.dateTime)


class _B:
    def __init__(self):
        self.g = Graph()
        self.n = 0

    def node(self, ns: str, kind: str, cls, raw: bool = True, **props) -> URIRef:
        self.n += 1
        s = uri(ns, kind, f"ref/{self.n:05d}")
        self.add(s, cls, raw=raw, **props)
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


def build_hero_reference(d=None) -> tuple[Graph, bool]:
    """(graph, has_retractions)."""
    d = d or load_derive()
    s = d.S
    b = _B()
    g = b.g
    apps = d.APPS
    for a in s.catalog:
        t = team_uri(a.owner_team)
        b.add(t, OPS.Team)
        g.add((t, RDFS.label, Literal(a.owner_team)))
        b.add(app_uri(a.id), OPS.Application, ops__inCatalog=Literal(True),
              ops__ownedBy=t, ops__appKind=Literal(a.kind), ops__tier=Literal(a.tier),
              ops__deployedIn=[region_uri(r) for r in a.regions])
        g.add((app_uri(a.id), RDFS.label, Literal(a.id)))
        for r in a.regions:
            b.add(region_uri(r), OPS.Region)
            g.add((region_uri(r), RDFS.label, Literal(r)))
    for e in s.edges:
        g.add((app_uri(e.caller), OPS.dependsOn, app_uri(e.callee)))

    # Changes: every rendered change record, under its external id.
    for c in d.rendered_changes():
        props = dict(ops__changeKind=concept("change_kind", c.kind), ops__atTime=_ts(c.at_utc),
                     chg__changeId=Literal(c.external_id))
        if c.app is not None:
            props["ops__changedApp"] = app_uri(c.app)
        b.add(change_uri(s.name, c.kind, c.external_id), _CLASS[c.kind], **props)

    def person(h):
        p = person_uri(h)
        b.add(p, OPS.Person)
        g.add((p, TEAM.handle, Literal(h)))
        return p

    # Incidents (history and live) as rendered.
    incs = {}
    for i in d.rendered_incidents().values():
        u = scoped_uri("inc", "incident", s.name, i.id)
        incs[i.id] = u
        props = dict(ops__affects=[app_uri(x) for x in i.affected],
                     ops__severity=concept("severity", i.severity), ops__atTime=_ts(i.opened),
                     inc__incidentId=Literal(i.id))
        if i.previous:
            props["inc__previouslyAffected"] = [app_uri(x) for x in i.previous]
        if i.resolved_by:
            props["ops__resolvedBy"] = person(i.resolved_by)
        if i.cause_kind:
            props["ops__causeKind"] = concept("change_kind", i.cause_kind)
        if i.cause_app:
            props["ops__causeApp"] = app_uri(i.cause_app)
        b.add(u, OPS.Incident, **props)
    live_i = d.live_incident()
    live = incs[live_i.id]
    rc = next(c for c in s.changes if c.id == s.answer_key.root_cause_change_id)
    similar = [x for x in d.rendered_incidents().values()
               if (x.cause_kind, x.cause_app) == (rc.kind, rc.app) and x.id != live_i.id]
    hist_sorted = sorted((x for x in d.rendered_incidents().values() if x.id != live_i.id),
                         key=lambda x: x.opened)
    for x in [*similar, hist_sorted[-1]]:      # the last one is a distractor outside last quarter
        g.add((live, OPS.similarTo, incs[x.id]))
        g.add((incs[x.id], OPS.similarTo, live))

    # Alerts and pages as rendered (one node per source id).
    for aid, (app, at) in d.alerts().items():
        b.add(scoped_uri("alrt", "alert", s.name, aid), OPS.Alert,
              ops__onApplication=app_uri(app), ops__atTime=_ts(at))
    for pid, (app, at) in d.pages().items():
        b.add(scoped_uri("alrt", "page", s.name, pid), OPS.Page,
              ops__onApplication=app_uri(app), ops__atTime=_ts(at))

    # Signals from propagation.
    for x in d.DEG:
        if apps[x.app].kind == "client":
            series = b.node("met", "series", OPS.MetricSeries, ops__onApplication=app_uri(x.app),
                            met__metricName=Literal("client.ttr.p90"))
            b.node("met", "anomaly", OPS.Anomaly, ops__onSeries=series,
                   ops__onApplication=app_uri(x.app), ops__atTime=_ts(x.start_utc),
                   ops__magnitude=Literal(Decimal(str(x.magnitude))))
        elif x.kind in _LOG_CAT:
            pat = b.node("log", "pattern", OPS.LogPattern, ops__onApplication=app_uri(x.app),
                         ops__errorCategory=concept("error_category", _LOG_CAT[x.kind]))
            b.node("log", "spike", OPS.LogSpike, ops__ofPattern=pat, ops__onApplication=app_uri(x.app),
                   ops__atTime=_ts(x.start_utc), ops__windowStart=_ts(x.start_utc),
                   ops__windowEnd=_ts(x.start_utc + timedelta(minutes=10)), ops__count=Literal(120),
                   ops__magnitude=Literal(Decimal("8.0")))
    # Distractors: a chronic NPE pattern (not a spike), a pre-window timeout spike,
    # a post-cutoff timeout spike, a pre-window alert and page, a late client anomaly.
    b.node("log", "pattern", OPS.LogPattern, ops__onApplication=app_uri("artwork-svc"),
           ops__errorCategory=concept("error_category", "null_pointer"))
    old = b.node("log", "pattern", OPS.LogPattern, ops__onApplication=app_uri("search-svc"),
                 ops__errorCategory=concept("error_category", "timeout"))
    b.node("log", "spike", OPS.LogSpike, ops__ofPattern=old, ops__onApplication=app_uri("search-svc"),
           ops__atTime=_ts(pt(2026, 10, 14, 9, 0)))
    late = b.node("log", "pattern", OPS.LogPattern, ops__onApplication=app_uri("profile-svc"),
                  ops__errorCategory=concept("error_category", "timeout"))
    b.node("log", "spike", OPS.LogSpike, ops__ofPattern=late, ops__onApplication=app_uri("profile-svc"),
           ops__atTime=_ts(d.AS_OF + timedelta(minutes=5)))
    b.node("alrt", "alert", OPS.Alert, ops__onApplication=app_uri("search-svc"),
           ops__atTime=_ts(pt(2026, 10, 14, 11, 0)))
    b.node("alrt", "page", OPS.Page, ops__onApplication=app_uri("search-svc"),
           ops__atTime=_ts(d.AS_OF + timedelta(minutes=10)))

    # Chat: the live thread, one message per authored beat, claims by phase.
    thread = scoped_uri("chat", "thread", s.name, d.live_beats()[0].channel)
    b.add(thread, CHAT.Thread, chat__category=concept("message_category", "incident"),
          chat__forIncident=live)
    claim_of = {}

    def message(at, handle):
        return b.node("chat", "message", OPS.Message, chat__inThread=thread, ops__atTime=_ts(at),
                      chat__category=concept("message_category", "incident"),
                      chat__sentiment=concept("sentiment", "neutral"),
                      chat__isIncidentRelated=Literal(True),
                      chat__classificationStatus=Literal("classified"),
                      chat__postedBy=person(handle))

    def claim(at, handle, kind, about, retracts=None):
        msg = message(at, handle)
        cls = {"mitigation": (OPS.Claim, OPS.Mitigation),
               "hypothesis": (OPS.Claim, OPS.Hypothesis)}.get(kind, OPS.Claim)
        props = dict(chat__claimKind=concept("claim_kind", kind), ops__atTime=_ts(at))
        if about:
            props["chat__about"] = [app_uri(x) for x in about]
        if retracts is not None:
            props["chat__retracts"] = retracts
        c = b.node("chat", "claim", cls, raw=False, **props)
        g.add((c, PROV.wasDerivedFrom, msg))
        return c

    for bt in d.live_beats():
        kind = _CLAIM_KIND.get(bt.phase)
        about = [x for x in apps if d.mentions(bt.text, x)]
        if kind and about:
            claim_of[bt.at] = claim(bt.at, bt.handle, kind, about)
    has_retractions = True
    try:
        rets = d.retractions(d.DEFAULT_ANSWERS)
    except d.GeneratorOutputMissing:
        has_retractions, rets = False, []
    beat_at = {bt.at: bt for bt in d.live_beats()}
    for r in rets:
        target = claim_of.get(r.claim_at)
        if target is None:                       # a verdict or question: still a claim to retract
            bt = beat_at[r.claim_at]
            target = claim_of[r.claim_at] = claim(bt.at, bt.handle, "observation", [r.app])
        claim(r.at, beat_at[r.claim_at].handle, "retraction", [r.app], retracts=target)

    # The twin's channel last quarter: a mitigation and a retraction, both before the window.
    twin = next(x for x in similar if x.opened < s.window.start_utc)
    m = claim(twin.opened + timedelta(minutes=20), "priya.nair", "mitigation", ["title-metadata-svc"])
    claim(twin.opened + timedelta(minutes=25), "priya.nair", "retraction", ["artwork-svc"], retracts=m)

    # History pattern (the misleading prior), from rendered postmortems.
    hist = [x for x in d.rendered_incidents().values() if x.id != live_i.id]
    sev2 = [x for x in hist if x.severity == "SEV2"]
    gw = [x for x in sev2 if (x.cause_app, x.cause_kind) == ("api-gateway", "deployment")]
    b.node("ops", "pattern", OPS.Pattern, ops__onApplication=app_uri("api-gateway"),
           ops__causeKind=concept("change_kind", "deployment"),
           ops__severity=concept("severity", "SEV2"), ops__support=Literal(len(gw)),
           ops__frequency=Literal(Decimal(len(gw)) / Decimal(len(sev2))))

    # Causal chains: the answer-key chain and a weaker distractor.
    def chain(links, conf):
        ch = b.node("ops", "chain", OPS.CausalChain, raw=False, ops__explains=live,
                    ops__confidence=Literal(conf))
        g.add((ch, PROV.wasDerivedFrom, live))
        for k, (sig, app, at) in enumerate(links):
            ln = b.node("ops", "link", OPS.CausalLink, raw=False, ops__linkOrder=Literal(k),
                        ops__onApplication=app_uri(app), ops__atTime=_ts(at),
                        ops__signalType=concept("signal_type", sig), ops__cites=live)
            g.add((ch, OPS.hasLink, ln))
    chain([(x.signal, x.app, x.at_utc) for x in s.answer_key.causal_chain], Decimal("0.82"))
    chain([("deployment", "api-gateway", pt(2026, 10, 14, 15, 18)),
           ("incident", "homepage-graphql", live_i.opened)], Decimal("0.61"))
    return g, has_retractions


@pytest.fixture(scope="session")
def derive_module():
    return load_derive()


@pytest.fixture(scope="session")
def hero_reference_built(derive_module):
    return build_hero_reference(derive_module)


@pytest.fixture(scope="session")
def hero_reference(hero_reference_built) -> Graph:
    return hero_reference_built[0]
