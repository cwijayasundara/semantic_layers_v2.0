"""Correlation enricher: the causal chains that explain the live incident.

Signals are drawn from the graphs earlier enrichers wrote (so an as-of store yields
only what was observable at its cutoff):

  signal type      drawn from                                          app
  ---------------  --------------------------------------------------  ---------------------
  deployment       ops:Deployment, ops:ClientRelease (with ops:atTime)  ops:changedApp
  config_change    ops:ConfigChange                                    ops:changedApp
  autoscaling      ops:ScalingEvent                                    ops:changedApp
  experiment       ops:ExperimentAllocation                            ops:changedApp
  commit           ops:Commit                                          ops:changedApp
  alert            ops:Alert (firing time)                             ops:onApplication
  incident         ops:Incident (creation time)                        ops:affects
  metric_anomaly   ops:Anomaly                                         ops:onApplication
  log_error        ops:LogSpike of a pattern with an ops:errorCategory  ops:onApplication
  slow_trace       ops:TraceEdgeFingerprint, signal type slow_trace     trc:caller, else callee
  failed_trace     ops:TraceEdgeFingerprint, signal type failed_trace   trc:caller, else callee

Not drawn: e2e_bottleneck (no enricher emits it) and error_pattern (an
ops:LogPattern's time is when it was first seen in the window, not an onset).

Fingerprints fold into onsets (`onsets`): a detection inside a running episode of its
series family (a metric name, a log pattern, a trace edge) is that condition seen
again, and episodes of one (type, app) that start in the same minute are one event.
Each onset is one Signal at its earliest detection, with the highest severity, citing
every detection. A trace edge's app is its caller, the application that waits on the
slow or failing call.

Severity squashes a fingerprint's magnitude by the magnitude its detector fires at,
m / (m + m_fire), so a signal at its threshold is 0.5 and a far larger one nears 1.
Changes, alerts and incidents are declared events, not estimates: severity 1.

The symptom is the harvest's (HarvestContext.symptom: the live incident, or in a
window with none, its most severe alert; `asof.live_symptom`). It is the only incident
drawn as a signal. Each chain is an ops:CausalChain that ops:explains it, with
ops:confidence, prov:wasDerivedFrom each linked signal, and one ops:CausalLink per
position (ops:linkOrder from 0 at the root, ops:signalType, ops:onApplication,
ops:atTime, ops:cites every detection behind the signal)."""
from collections import defaultdict
from datetime import datetime

from rdflib import URIRef
from rdflib.namespace import RDF

from e2ekg.correlate.engine import DEFAULT_PRIOR, PRIORS, Chain, Signal, correlate
from e2ekg.graph.enricher import NS, Enricher, dec, integer, node, ts
from e2ekg.graph.enrichers.traces import SLOW_RATIO
from e2ekg.graph.uris import app_uri, concept
from e2ekg.preprocess.identity import APP_URI_PREFIX
from e2ekg.preprocess.logtemplates import Z_MIN as LOG_SPIKE_Z
from e2ekg.preprocess.metrics_clean import SHIFT_MIN
from e2ekg.scenario.model import CHANGE_SIGNAL, SIGNAL_TYPES

OPS, PROV = NS["ops"], NS["prov"]
FIRES_AT = {"metric_anomaly": SHIFT_MIN, "log_error": LOG_SPIKE_Z,
            "slow_trace": SLOW_RATIO, "failed_trace": SLOW_RATIO}
_PFX = """PREFIX ops: <https://kg.local/ops#>
PREFIX trc: <https://kg.local/trc#>
PREFIX met: <https://kg.local/met#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""
_EVENTS = _PFX + """SELECT ?s ?type ?app ?t WHERE {
  { ?s a/rdfs:subClassOf ops:Change ; ops:changeKind/skos:notation ?type ; ops:atTime ?t .
    OPTIONAL { ?s ops:changedApp ?app } }
  UNION { ?s a ops:Alert ; ops:atTime ?t . BIND("alert" AS ?type)
          OPTIONAL { ?s ops:onApplication ?app } }
}"""
_INCIDENT = _PFX + """SELECT ?t (MIN(STR(?a)) AS ?app) WHERE {
  <%s> a ops:Incident ; ops:atTime ?t . OPTIONAL { <%s> ops:affects ?a } } GROUP BY ?t"""
_FINGERPRINTS = _PFX + """SELECT ?s ?type ?family ?app ?caller ?t ?end ?m WHERE {
  { ?s a ops:Anomaly ; ops:onSeries/met:metricName ?family . BIND("metric_anomaly" AS ?type) }
  UNION { ?s a ops:LogSpike ; ops:ofPattern ?family . ?family ops:errorCategory ?c .
          BIND("log_error" AS ?type) }
  UNION { ?s a ops:TraceEdgeFingerprint ; ops:signalType/skos:notation ?type ; trc:onEdge ?family .
          OPTIONAL { ?s trc:caller ?caller } }
  ?s ops:atTime ?t ; ops:windowEnd ?end .
  OPTIONAL { ?s ops:onApplication ?app }
  OPTIONAL { ?s ops:magnitude ?m }
}"""


def _app(u: str | None) -> str | None:
    return u[len(APP_URI_PREFIX):] if u and u.startswith(APP_URI_PREFIX) else None


def severity(kind: str, magnitude: float | None) -> float:
    m_fire = FIRES_AT[kind]
    m = m_fire if magnitude is None else max(magnitude, 0.0)
    return m / (m + m_fire)


def onsets(rows: list[dict]) -> list[Signal]:
    """Fingerprint rows -> one Signal per (type, app, minute its episode began)."""
    families: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        families[(r["type"], r["app"], r["family"])].append(r)
    events: dict[tuple, list[dict]] = defaultdict(list)
    for (kind, app, _), group in families.items():
        began, end = None, None
        for r in sorted(group, key=lambda r: (r["t"], r["s"])):
            if end is None or r["t"] >= end:
                began, end = r["t"].replace(second=0, microsecond=0), r["end"]
            end = max(end, r["end"])
            events[(kind, app, began)].append(r)
    out = []
    for (kind, app, _), rs in events.items():
        first = min(rs, key=lambda r: (r["t"], r["s"]))
        out.append(Signal(uri=first["s"], type=kind, app=app, at_utc=first["t"],
                          severity=max(severity(kind, r["m"]) for r in rs),
                          evidence=tuple(sorted({r["s"] for r in rs}))))
    return out


def signals_from_store(store, symptom: str | None = None) -> list[Signal]:
    """Every change, alert and telemetry onset in the store, and the symptom if it is an
    incident. No other incident is a signal: history incidents explain nothing live."""
    out = []
    rows = store.query(_INCIDENT % (symptom, symptom)) if symptom else []
    for r in rows:
        out.append(Signal(uri=symptom, type="incident", app=_app(r["app"]),
                          at_utc=datetime.fromisoformat(r["t"]), severity=1.0,
                          evidence=(symptom,)))
    for r in store.query(_EVENTS):
        kind = CHANGE_SIGNAL.get(r["type"], r["type"])
        out.append(Signal(uri=r["s"], type=kind, app=_app(r["app"]),
                          at_utc=datetime.fromisoformat(r["t"]), severity=1.0,
                          evidence=(r["s"],)))
    rows = [{"s": r["s"], "type": r["type"], "family": r["family"],
             "app": _app(r["caller"] or r["app"]), "t": datetime.fromisoformat(r["t"]),
             "end": datetime.fromisoformat(r["end"]),
             "m": None if r["m"] is None else float(r["m"])}
            for r in store.query(_FINGERPRINTS) if r["type"] in FIRES_AT]
    return sorted(set(out) | set(onsets(rows)), key=lambda s: (s.at_utc, s.uri))


def dependency_edges(store) -> list[tuple[str, str]]:
    rows = store.query(_PFX + "SELECT ?a ?b WHERE { ?a ops:dependsOn ?b }")
    return sorted({(_app(r["a"]), _app(r["b"])) for r in rows if _app(r["a"]) and _app(r["b"])})


def chain_inputs(store, symptom: str | None) -> tuple[list[Signal], list[tuple[str, str]],
                                                     str | None]:
    """(signals, edges, symptom uri): the arguments of `correlate` over a store."""
    return signals_from_store(store, symptom), dependency_edges(store), symptom


def priors_with_history(store) -> dict[tuple[str, str], float]:
    """PRIORS raised by learned ops:Patterns: a change type whose strongest pattern has
    frequency f (the share of incidents of a severity it caused) gets, for every
    effect type, the noisy-or 1 - (1 - prior)(1 - f)."""
    rows = store.query(_PFX + """SELECT ?kind (MAX(?f) AS ?freq) WHERE {
        ?p a ops:Pattern ; ops:causeKind/skos:notation ?kind ; ops:frequency ?f } GROUP BY ?kind""")
    out = dict(PRIORS)
    for r in rows:
        cause, f = CHANGE_SIGNAL.get(r["kind"]), float(r["freq"])
        if cause is None:
            continue
        for effect in SIGNAL_TYPES:
            p = out.get((cause, effect), DEFAULT_PRIOR)
            out[(cause, effect)] = 1 - (1 - p) * (1 - f)
    return out


class CorrelationEnricher(Enricher):
    name = "correlation"
    reads = ("graph:topology", "graph:changes", "graph:metrics", "graph:logs", "graph:traces",
             "graph:alerts_incidents")

    def fetch(self, ctx):
        return chain_inputs(ctx.store, ctx.symptom)

    def analyze(self, raw):
        signals, edges, symptom = raw
        return symptom, (correlate(signals, edges, symptom) if symptom else [])

    def emit(self, result):
        symptom, chains = result
        for c in chains:
            yield from _chain(self.scenario, URIRef(symptom), c)


def _chain(scenario: str, symptom: URIRef, c: Chain):
    u = node("ops", "chain", scenario, *(s.uri for s in c.links))
    yield u, RDF.type, OPS.CausalChain
    yield u, OPS.explains, symptom
    yield u, OPS.confidence, dec(round(c.confidence, 6))
    for i, s in enumerate(c.links):
        link = node("ops", "link", scenario, u, i)
        yield u, OPS.hasLink, link
        yield u, PROV.wasDerivedFrom, URIRef(s.uri)
        yield link, RDF.type, OPS.CausalLink
        yield link, OPS.linkOrder, integer(i)
        yield link, OPS.signalType, concept("signal_type", s.type)
        yield link, OPS.atTime, ts(s.at_utc)
        yield link, PROV.wasDerivedFrom, URIRef(s.uri)
        if s.app:
            yield link, OPS.onApplication, app_uri(s.app)
        for e in s.evidence:
            yield link, OPS.cites, URIRef(e)
