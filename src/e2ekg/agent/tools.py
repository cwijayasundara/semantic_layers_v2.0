"""The RCA agent's whole view of the world: typed tools over the knowledge graph.

    tools = make_tools(store, proc_root, scenario, specialist)

`store` is the AS-OF store the live RCA reads (R34, `harvest --as-of`): it holds only what
was observable at the cutoff, so a graph tool needs no time filter of its own and nothing
after the cutoff can be returned by one. Two tools reach past the graph and so enforce the
cutoff themselves:

  * `raw_evidence` reads the processed tables and the raw files, which hold the whole
    window. It returns a record only when EVERY row parsed from it is stamped before the
    cutoff, whatever the request (a node's selectors, a selector the agent writes, a raw id).
  * `sparql_select` runs on `store` alone. The full store is never joined to it.

Every result is compact JSON of at most CAP characters. A list that does not fit loses its
tail, and its last row says `"truncated": n`; one whose query hit its LIMIT says `"capped": 200`.
A call that cannot be answered returns "error: <what to change>" instead of raising, so the
model can correct itself. App arguments go through the `Resolver`, so "TMS" works when the
tools were made with an `llm`.

Times are ISO 8601 (`2026-10-14T21:40Z`); a window is [start, end] on the stamp the node
carries as ops:atTime (a change's time, a fingerprint's onset or window start). Every URI a
result names can be passed back to `record_finding`, `raw_evidence`, `causal_chains` and
`sparql_select`."""
import difflib
import functools
import hashlib
import json
import re
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import duckdb
import polars as pl
import pyarrow as pa
import pyshacl
from langchain_core.tools import BaseTool, tool
from rdflib import Graph, Literal as RdfLiteral, URIRef
from rdflib.namespace import RDF, RDFS

from e2ekg.agent.refute import Call, Investigation
from e2ekg.agent.report import FindingIn
from e2ekg.correlate.engine import CHANGE_TYPES, correlate
from e2ekg.graph.enricher import dec, node
from e2ekg.graph.enrichers.correlation import chain_inputs
from e2ekg.graph.harvest import symptom_uri
from e2ekg.graph.shacl import load_ontology, load_shapes
from e2ekg.graph.store import Store, graph_uri
from e2ekg.graph.uris import BASE, CHANGE_KINDS, NS, app_uri, concept, scoped_uri
from e2ekg.preprocess import asof
from e2ekg.preprocess import selector as sel
from e2ekg.preprocess.identity import APP_URI_PREFIX, Resolver, catalog_from_table
from e2ekg.preprocess.tables import TABLES
from e2ekg.rawid import RawSpan

CAP = 4000
LIMIT = 200
LINE_MAX = 1500
MAX_CHAINS = 5
CLAIM_KINDS = ("observation", "hypothesis", "mitigation", "root_cause", "retraction")

OPS, PROV = NS["ops"], NS["prov"]
_PREFIXES = {**{p: str(ns) for p, ns in NS.items()},
             "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
             "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
             "xsd": "http://www.w3.org/2001/XMLSchema#"}
PFX = "".join(f"PREFIX {p}: <{ns}>\n" for p, ns in _PREFIXES.items())
_IRI = re.compile(rf"{re.escape(BASE)}/[A-Za-z0-9._~@:/+#-]+")
_RAW_ID = re.compile(r"[^|]+\|.+\|\d+\|\d+\|[0-9a-f]{16}")
_INCIDENT_ID = re.compile(r"(?i)INC-\d+")
_SPECIALIST = re.compile(r"[A-Za-z0-9_-]+")
_SOURCE_TABLES = {"events": ("events", "alerts", "pages", "incidents"), "chat": ("slack",),
                  "catalog": ("catalog",), "logs": ("logs",), "atlas": ("metrics_atlas",),
                  "prom": ("metrics_prom",), "traces": ("spans",)}


class ToolError(Exception):
    """A call the model can fix: a bad argument or unknown name. Returned as "error: ..."."""


@dataclass
class Env:
    store: Store
    proc: Path
    raw: Path
    scenario: str
    specialist: str
    resolver: Resolver
    as_of: datetime
    symptom: str | None
    chains: dict[str, str] = field(default_factory=dict)


def _guarded(fn):
    @functools.wraps(fn)
    def run(*a, **k) -> str:
        try:
            return fn(*a, **k)
        except ToolError as e:
            return f"error: {e}"
    return run


# --- shared helpers ----------------------------------------------------------------------

def _dump(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _fit(rows: list, capped: bool = False, head: dict | None = None, key: str = "rows") -> str:
    """The longest prefix of `rows` whose JSON is at most CAP characters. What was left out is
    noted on the last row kept ({"truncated": n} rows, {"capped": 200} when the query hit its
    LIMIT), so every element of the list stays a row. `head` makes the result an object
    {**head, key: rows, notes}."""
    def render(k: int) -> str:
        notes = {}
        if k < len(rows):
            notes["truncated"] = len(rows) - k
        if capped:
            notes["capped"] = LIMIT
        shown = [dict(r) for r in rows[:k]]
        if head is not None:
            return _dump({**head, key: shown, **notes})
        if notes:
            shown[-1:] = [{**shown[-1], **notes}] if shown else [notes]
        return _dump(shown)

    if len(whole := render(len(rows))) <= CAP:
        return whole
    lo, hi = 0, len(rows)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if len(render(mid)) <= CAP else (lo, mid - 1)
    return render(lo)


def _when(s: str) -> datetime:
    try:
        t = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(f"cannot read the time {s!r}; write ISO 8601, e.g. 2026-10-14T21:40Z") \
            from None
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(UTC)


def _z(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _window(start: str, end: str, var: str = "?t") -> str:
    a, b = _when(start), _when(end)
    if a > b:
        raise ToolError(f"start {start} is after end {end}")
    return (f'FILTER({var} >= "{_z(a)}"^^xsd:dateTime && {var} <= "{_z(b)}"^^xsd:dateTime)')


def _iri(u: str) -> str:
    if not _IRI.fullmatch(u.strip()):
        raise ToolError(f"{u!r} is not a graph node URI (https://kg.local/...)")
    return f"<{u.strip()}>"


def _select(env: Env, body: str) -> list[dict]:
    return env.store.query(PFX + body)


def _exists(env: Env, u: str) -> bool:
    return bool(env.store.query(f"ASK {{ {_iri(u)} ?p ?o }}"))


def _app_id(env: Env, name: str) -> str:
    app = env.resolver.resolve(name).app
    if app is None:
        near = difflib.get_close_matches(name.lower(), env.resolver.ids, n=3, cutoff=0.4)
        raise ToolError(f"unknown application {name!r}"
                        + (f"; did you mean {', '.join(near)}?" if near else ""))
    return str(app)


def _apps_values(env: Env, apps: list[str], var: str = "?app") -> str:
    """A VALUES clause for the resolved apps; no apps means every application."""
    ids = sorted({_app_id(env, a) for a in apps})
    return f"VALUES {var} {{ {' '.join(f'<{app_uri(i)}>' for i in ids)} }}" if ids else ""


def _app_of(u: str | None) -> str | None:
    return u[len(APP_URI_PREFIX):] if u and u.startswith(APP_URI_PREFIX) else u


def _tail(u: str) -> str:
    return u.rsplit("/", 1)[-1]


def _clip(s: str | None, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n - 1] + "…"


def _num(x: str | None, places: int = 2):
    if x is None:
        return None
    v = round(float(x), places)
    return int(v) if v == int(v) else v


def _kinds(codes: list[str], scheme: str, valid: tuple[str, ...]) -> list[str]:
    out = []
    for c in codes:
        k = c.strip().lower().replace("-", "_").replace(" ", "_")
        if k not in valid:
            raise ToolError(f"unknown {scheme} {c!r}; valid: {', '.join(valid)}")
        out.append(k)
    return out


# --- graph tools -------------------------------------------------------------------------

@_guarded
def find_symptom(env: Env, alert_id: str) -> str:
    ident = alert_id.strip()
    if _INCIDENT_ID.fullmatch(ident):
        inc = _select(env, f"""SELECT DISTINCT ?a WHERE {{
            ?i inc:incidentId "{ident.upper()}" ; inc:openedBy/ops:forAlert ?a }} LIMIT {LIMIT}""")
        if not inc:
            raise ToolError(f"no alert opened incident {ident} before the cutoff")
        ident = _tail(inc[0]["a"])
    if not re.fullmatch(r"[A-Za-z0-9._-]+", ident):
        raise ToolError(f"{alert_id!r} is not an alert id such as radar-09000")
    a = str(scoped_uri("alrt", "alert", env.scenario, ident))
    row = _select(env, f"""SELECT ?app ?t WHERE {{ <{a}> a ops:Alert ; ops:atTime ?t .
        OPTIONAL {{ <{a}> ops:onApplication ?app }} }} LIMIT {LIMIT}""")
    if not row:
        known = [_tail(r["a"]) for r in _select(env, f"""SELECT ?a ?t WHERE {{
            ?a a ops:Alert ; ops:atTime ?t }} ORDER BY DESC(?t) LIMIT 30""")]
        raise ToolError(f"no alert {alert_id!r} before the cutoff; alerts seen: {', '.join(known)}")
    table = asof.scan(env.proc, "alerts", env.as_of).filter(pl.col("alert_id") == ident) \
        .sort("ts_utc", "raw_id").collect()
    f = json.loads(table["fields"][0]) if table.height else {}
    alert = {"uri": a, "id": ident, "app": _app_of(row[0]["app"]), "at": row[0]["t"],
             "severity": f.get("severity"), "metric": f.get("metric"),
             "summary": _clip(f.get("summary"), 200)}
    opened = _select(env, f"""SELECT ?i ?id ?t ?sev ?aff WHERE {{
        ?pg ops:forAlert <{a}> . ?i inc:openedBy ?pg ; inc:incidentId ?id ; ops:atTime ?t .
        OPTIONAL {{ ?i ops:severity ?sev }} OPTIONAL {{ ?i ops:affects ?aff }} }} LIMIT {LIMIT}""")
    incident = None
    if opened:
        o = opened[0]
        incident = {"uri": o["i"], "id": o["id"], "at": o["t"], "severity": _tail(o["sev"] or ""),
                    "affects": sorted({_app_of(r["aff"]) for r in opened if r["aff"]})}
    return _dump({"symptom_uri": incident["uri"] if incident else a, "alert": alert,
                  "incident": incident, "as_of": _z(env.as_of)})


@_guarded
def dependency_closure(env: Env, app: str, direction: str) -> str:
    start = _app_id(env, app)
    step = (lambda u: f"<{u}> ops:dependsOn ?n") if direction == "down" else \
        (lambda u: f"?n ops:dependsOn <{u}>")
    hops, frontier = {start: 0}, [start]
    while frontier:
        nxt = []
        for a in frontier:
            q = f"SELECT DISTINCT ?n WHERE {{ {step(app_uri(a))} }} LIMIT {LIMIT}"
            for r in _select(env, q):
                b = _app_of(r["n"])
                if b not in hops:
                    hops[b] = hops[a] + 1
                    nxt.append(b)
        frontier = nxt
    info = {_app_of(r["a"]): r for r in _select(env, f"""SELECT ?a ?kind ?tier WHERE {{
        VALUES ?a {{ {' '.join(f'<{app_uri(i)}>' for i in hops)} }}
        ?a ops:appKind ?kind ; ops:tier ?tier }} LIMIT {LIMIT}""")}
    rows = [{"id": a, "hops": h, "kind": info.get(a, {}).get("kind"),
             "tier": _num(info.get(a, {}).get("tier"))}
            for a, h in sorted(hops.items(), key=lambda x: (x[1], x[0]))]
    return _fit(rows)


def _change_what(r: dict) -> str:
    if r["prop"]:
        return f"{r['prop']} {r['old']}->{r['new']}"
    if r["dcap"]:
        return f"capacity {r['pcap']}->{r['dcap']} ({r['status']})"
    if r["version"]:
        return f"{r['version']} {r['status']}"
    if r["experiment"]:
        return f"{r['experiment']} cell {r['cell']} {r['oalloc']}%->{r['nalloc']}%"
    return ""


@_guarded
def changes_in_window(env: Env, apps: list[str], start: str, end: str) -> str:
    rows = _select(env, f"""SELECT ?c ?id ?kind ?app ?t ?prop ?old ?new ?pcap ?dcap ?version
        ?status ?experiment ?cell ?oalloc ?nalloc WHERE {{
        {_apps_values(env, apps)}
        ?c chg:changeId ?id ; ops:changeKind ?kind ; ops:changedApp ?app ; ops:atTime ?t .
        OPTIONAL {{ ?c chg:property ?prop ; chg:oldValue ?old ; chg:newValue ?new }}
        OPTIONAL {{ ?c chg:desiredCapacity ?dcap ; chg:previousCapacity ?pcap }}
        OPTIONAL {{ ?c chg:version ?version }} OPTIONAL {{ ?c chg:status ?status }}
        OPTIONAL {{ ?c exp:experiment ?experiment ; exp:cell ?cell ;
                      exp:oldAllocationPct ?oalloc ; exp:newAllocationPct ?nalloc }}
        {_window(start, end)} }} ORDER BY ?t ?c LIMIT {LIMIT}""")
    out = [{"id": r["id"], "uri": r["c"], "kind": _tail(r["kind"]), "app": _app_of(r["app"]),
            "at": r["t"], "what": _change_what(r)} for r in rows]
    return _fit(out, capped=len(rows) >= LIMIT)


@_guarded
def anomalies_in_window(env: Env, apps: list[str], start: str, end: str) -> str:
    rows = _select(env, f"""SELECT ?s ?app ?t ?m ?dir ?name WHERE {{
        {_apps_values(env, apps)}
        ?s a ops:Anomaly ; ops:onApplication ?app ; ops:atTime ?t ;
           ops:onSeries/met:metricName ?name .
        OPTIONAL {{ ?s ops:magnitude ?m }} OPTIONAL {{ ?s met:direction ?dir }}
        {_window(start, end)} }} ORDER BY ?t ?s LIMIT {LIMIT}""")
    groups: dict[tuple, dict] = {}
    for r in rows:
        g = groups.setdefault((r["app"], r["name"], r["dir"]), {
            "uri": r["s"], "app": _app_of(r["app"]), "metric": r["name"], "dir": r["dir"],
            "at": r["t"], "mag": None, "series": 0})
        g["series"] += 1
        m = _num(r["m"])
        g["mag"] = m if g["mag"] is None or (m is not None and m > g["mag"]) else g["mag"]
    return _fit(list(groups.values()), capped=len(rows) >= LIMIT)


@_guarded
def log_spikes(env: Env, apps: list[str], start: str, end: str) -> str:
    rows = _select(env, f"""SELECT ?s ?app ?t ?n ?z ?tpl ?cat WHERE {{
        {_apps_values(env, apps)}
        ?s a ops:LogSpike ; ops:onApplication ?app ; ops:atTime ?t ; ops:ofPattern ?p .
        ?p log:template ?tpl . OPTIONAL {{ ?s ops:count ?n }} OPTIONAL {{ ?s ops:magnitude ?z }}
        OPTIONAL {{ ?p ops:errorCategory ?cat }}
        {_window(start, end)} }} ORDER BY ?t ?s LIMIT {LIMIT}""")
    out = [{"uri": r["s"], "app": _app_of(r["app"]), "at": r["t"], "n": _num(r["n"]),
            "z": _num(r["z"]), "cat": _tail(r["cat"]) if r["cat"] else None,
            "tpl": _clip(r["tpl"], 90)} for r in rows]
    return _fit(out, capped=len(rows) >= LIMIT)


@_guarded
def trace_edges(env: Env, apps: list[str], start: str, end: str) -> str:
    ids = sorted({_app_id(env, a) for a in apps})
    values = ' '.join(f'<{app_uri(i)}>' for i in ids)
    touching = (f"VALUES ?x {{ {values} }} {{ ?s ops:onApplication ?x }} "
                "UNION { ?s trc:caller ?x }") if ids else ""
    rows = _select(env, f"""SELECT DISTINCT ?s ?caller ?callee ?type ?t ?n ?m ?p50 ?p99 ?err
        WHERE {{
        {touching}
        ?s a ops:TraceEdgeFingerprint ; ops:signalType ?type ; ops:atTime ?t .
        OPTIONAL {{ ?s ops:onApplication ?callee }} OPTIONAL {{ ?s trc:caller ?caller }}
        OPTIONAL {{ ?s ops:count ?n }} OPTIONAL {{ ?s ops:magnitude ?m }}
        OPTIONAL {{ ?s trc:p50Ms ?p50 }} OPTIONAL {{ ?s trc:p99Ms ?p99 }}
        OPTIONAL {{ ?s trc:errorRate ?err }}
        {_window(start, end)} }} ORDER BY ?t ?s LIMIT {LIMIT}""")
    out = [{"uri": r["s"], "caller": _app_of(r["caller"]), "callee": _app_of(r["callee"]),
            "type": _tail(r["type"]), "at": r["t"], "n": _num(r["n"]), "mag": _num(r["m"]),
            "p50": _num(r["p50"]), "p99": _num(r["p99"]), "err": _num(r["err"], 3)}
           for r in rows]
    return _fit(out, capped=len(rows) >= LIMIT)


@_guarded
def alerts_incidents(env: Env, start: str, end: str) -> str:
    alerts = _select(env, f"""SELECT ?a ?app ?t WHERE {{ ?a a ops:Alert ; ops:atTime ?t .
        OPTIONAL {{ ?a ops:onApplication ?app }} {_window(start, end)} }}
        ORDER BY ?t ?a LIMIT {LIMIT}""")
    incidents = _select(env, f"""SELECT ?i ?id ?t ?sev (GROUP_CONCAT(DISTINCT STR(?aff);
        SEPARATOR=" ") AS ?affs) (COUNT(DISTINCT ?by) AS ?resolved) WHERE {{
        ?i a ops:Incident ; inc:incidentId ?id ; ops:atTime ?t .
        OPTIONAL {{ ?i ops:severity ?sev }} OPTIONAL {{ ?i ops:affects ?aff }}
        OPTIONAL {{ ?i ops:resolvedBy ?by }} {_window(start, end)} }}
        GROUP BY ?i ?id ?t ?sev ORDER BY ?t ?i LIMIT {LIMIT}""")
    out = [{"kind": "alert", "uri": r["a"], "id": _tail(r["a"]), "app": _app_of(r["app"]),
            "at": r["t"]} for r in alerts]
    out += [{"kind": "incident", "uri": r["i"], "id": r["id"], "at": r["t"],
             "severity": _tail(r["sev"] or ""),
             "affects": sorted(_app_of(a) for a in (r["affs"] or "").split()),
             "resolved": r["resolved"] != "0"} for r in incidents]
    out.sort(key=lambda r: (r["at"], r["uri"]))
    return _fit(out, capped=max(len(alerts), len(incidents)) >= LIMIT)


@_guarded
def similar_incidents(env: Env, app: str, kinds: list[str]) -> str:
    cause = app_uri(_app_id(env, app))
    codes = _kinds(kinds, "change kind", CHANGE_KINDS)
    allowed = ' '.join(f"<{concept('change_kind', k)}>" for k in codes)
    kind_filter = f"VALUES ?kind {{ {allowed} }}" if codes else ""
    live = (f"OPTIONAL {{ <{env.symptom}> ops:similarTo ?i . BIND(1 AS ?lv) }}"
            if env.symptom else "")
    rows = _select(env, f"""SELECT ?i ?id ?t ?sev ?kind ?by (MAX(?lv) AS ?live)
        (GROUP_CONCAT(DISTINCT STR(?aff); SEPARATOR=" ") AS ?affs) WHERE {{
        {kind_filter}
        ?i a ops:Incident ; inc:incidentId ?id ; ops:atTime ?t ; ops:causeApp <{cause}> ;
           ops:causeKind ?kind ; ops:resolvedBy ?by .
        OPTIONAL {{ ?i ops:severity ?sev }} OPTIONAL {{ ?i ops:affects ?aff }} {live} }}
        GROUP BY ?i ?id ?t ?sev ?kind ?by ORDER BY DESC(?t) ?i LIMIT {LIMIT}""")
    patterns = _select(env, f"""SELECT ?kind ?sev ?sup ?freq WHERE {{ {kind_filter}
        ?p a ops:Pattern ; ops:onApplication <{cause}> ; ops:causeKind ?kind ;
           ops:severity ?sev ; ops:support ?sup ; ops:frequency ?freq }} ORDER BY ?kind ?sev
        LIMIT {LIMIT}""")
    incidents = [{"id": r["id"], "uri": r["i"], "at": r["t"], "severity": _tail(r["sev"] or ""),
                  "cause": _tail(r["kind"]), "by": _tail(r["by"]),
                  "affects": sorted(_app_of(a) for a in (r["affs"] or "").split()),
                  "like_live": r["live"] == "1"} for r in rows]
    head = {"patterns": [{"kind": _tail(p["kind"]), "severity": _tail(p["sev"]),
                          "support": _num(p["sup"]), "freq": _num(p["freq"], 3)}
                         for p in patterns]}
    return _fit(incidents, capped=len(rows) >= LIMIT, head=head, key="incidents")


@_guarded
def chat_claims(env: Env, channel: str, kinds: list[str]) -> str:
    ident = channel.strip()
    if _INCIDENT_ID.fullmatch(ident):
        found = _select(env, f"""SELECT DISTINCT ?th WHERE {{ ?th chat:forIncident ?i .
            ?i inc:incidentId "{ident.upper()}" }} LIMIT {LIMIT}""")
        thread = found[0]["th"] if found else None
    else:
        thread = ident if ident.startswith(BASE) else \
            str(scoped_uri("chat", "thread", env.scenario, ident.lstrip("#"))) \
            if re.fullmatch(r"[A-Za-z0-9._-]+", ident.lstrip("#")) else None
    if thread is None or not _exists(env, thread):
        raise ToolError(f"no chat channel {channel!r} before the cutoff")
    codes = _kinds(kinds, "claim kind", CLAIM_KINDS)
    allowed = ' '.join(f"<{concept('claim_kind', k)}>" for k in codes)
    rows = _select(env, f"""SELECT ?c ?kind ?about ?t ?by ?q ?ret WHERE {{
        {f"VALUES ?kind {{ {allowed} }}" if codes else ""}
        ?c a ops:Claim ; chat:claimKind ?kind ; prov:wasDerivedFrom ?m ; ops:atTime ?t .
        ?m chat:inThread <{thread}> .
        OPTIONAL {{ ?c chat:about ?about }} OPTIONAL {{ ?c chat:postedBy ?by }}
        OPTIONAL {{ ?c chat:quote ?q }} OPTIONAL {{ ?c chat:retracts ?ret }} }}
        ORDER BY ?t ?c LIMIT {LIMIT}""")
    out = [{"uri": r["c"], "kind": _tail(r["kind"]), "app": _app_of(r["about"]), "at": r["t"],
            "by": _tail(r["by"] or ""), "quote": _clip(r["q"], 110), "retracts": r["ret"]}
           for r in rows]
    return _fit(out, capped=len(rows) >= LIMIT)


@_guarded
def causal_chains(env: Env, symptom: str) -> str:
    if symptom not in env.chains:
        if not env.store.query(PFX + f"""ASK {{ {{ {_iri(symptom)} a ops:Incident }}
                                                UNION {{ {_iri(symptom)} a ops:Alert }} }}"""):
            raise ToolError(f"{symptom} is not an incident or alert in the graph before the "
                            "cutoff; find_symptom names the live one")
        signals, edges, _ = chain_inputs(env.store, symptom)
        chains = correlate(signals, edges, symptom)[:MAX_CHAINS]
        env.chains[symptom] = _fit([
            {"conf": round(c.confidence, 3),
             "links": [[s.type, s.app, _z(s.at_utc), s.uri] for s in c.links]} for c in chains])
    return env.chains[symptom]


@_guarded
def refutation_check(env: Env, alert_id: str, candidate_uris: list[str]) -> str:
    found = find_symptom(env, alert_id)
    if found.startswith("error: "):
        raise ToolError(found.removeprefix("error: "))
    sym = json.loads(found)
    signals, edges, _ = chain_inputs(env.store, sym["symptom_uri"])
    kinds = {_app_of(r["a"]): r["k"]
             for r in env.store.query(PFX + "SELECT ?a ?k WHERE { ?a ops:appKind ?k }")}
    calls = [Call(_app_of(r["caller"]), _app_of(r["callee"]), datetime.fromisoformat(r["t"]))
             for r in env.store.query(PFX + """SELECT ?caller ?callee ?t ?type WHERE {
                 ?s a ops:TraceEdgeFingerprint ; ops:signalType ?type ; ops:atTime ?t ;
                    ops:onApplication ?callee ; trc:caller ?caller }""")
             if _tail(r["type"]) in ("slow_trace", "failed_trace")]
    filed = (sym["incident"] or {}).get("affects", [])
    inv = Investigation(signals, edges, calls, kinds, sym["alert"]["uri"], filed,
                        correlate(signals, edges, sym["symptom_uri"]))
    changes = {s.uri for s in signals if s.type in CHANGE_TYPES}
    asked = [u.strip() for u in candidate_uris]
    verdicts = inv.judge(inv.candidates(u for u in asked if u in changes))
    change_kind = {r["c"]: _tail(r["k"]) for r in _select(env, f"""SELECT ?c ?k WHERE {{
        VALUES ?c {{ {' '.join(f'<{v.change.uri}>' for v in verdicts)} }}
        ?c ops:changeKind ?k }}""")} if verdicts else {}
    rows = []
    for v in verdicts:
        c = v.change
        row = {"uri": c.uri, "kind": change_kind.get(c.uri, c.type), "app": c.app,
               "at": _z(c.at_utc), "survives": v.survives,
               "refuted_by": [f"{k.name}: {k.why}" for k in v.checks if not k.ok]}
        if v.survives:
            row["supported_by"] = [f"{k.name}: {k.why}" for k in v.checks]
        rows.append({**row, "chain_conf": v.chain_conf})
    rows += [{"uri": u, "error": "not a change in the graph before the cutoff"}
             for u in asked if u not in changes]
    head = {"symptom": {
        "alert": inv.alert.uri, "app": inv.alert.app, "alert_at": _z(inv.alert.at_utc),
        "onset": _z(inv.onset.at_utc), "onset_uri": inv.onset.uri, "clients": list(inv.clients),
        "filed_against": list(inv.filed_against),
        "filed_in_closure": all(inv.depends(inv.alert.app, a) for a in filed) if filed else None}}
    return _fit(rows, head=head, key="candidates")


# --- sparql ------------------------------------------------------------------------------

_COMMENT = re.compile(r"""(<[^<>"\s]*>|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|#[^\n]*""")
_DECL = re.compile(r"(?is)^\s*(?:(?:PREFIX\s+[\w-]*:\s*<[^>]*>|BASE\s+<[^>]*>)\s*)*")
_TRAILING_LIMIT = re.compile(r"(?is)\bLIMIT\s+(\d+)\s*(OFFSET\s+\d+\s*)?$")


@_guarded
def sparql_select(env: Env, query: str) -> str:
    bare = _COMMENT.sub(lambda m: m.group(1) or "", query).strip()
    body = bare[_DECL.match(bare).end():]
    if not re.match(r"(?i)SELECT\b", body):
        raise ToolError("only SELECT queries are allowed (no ASK, CONSTRUCT, DESCRIBE or update)")
    if re.search(r"(?i)\bSERVICE\b", body):
        raise ToolError("SERVICE (federated queries) is not allowed; the graph is all there is")
    if m := _TRAILING_LIMIT.search(bare):
        bare = bare[:m.start()] + f"LIMIT {min(int(m.group(1)), LIMIT)} " + (m.group(2) or "")
    else:
        bare += f"\nLIMIT {LIMIT}"
    declared = set(re.findall(r"(?i)PREFIX\s+([\w-]*):", bare))
    prefixes = "".join(f"PREFIX {p}: <{ns}>\n" for p, ns in _PREFIXES.items()
                       if p not in declared)
    try:
        rows = env.store.query(prefixes + bare)
    except (SyntaxError, ValueError) as e:
        raise ToolError(f"SPARQL did not run: {e}") from None
    return _fit([{k: v for k, v in r.items()} for r in rows], capped=len(rows) >= LIMIT)


# --- raw evidence ------------------------------------------------------------------------

def _raw_root(proc: Path) -> Path:
    """<root>/processed/<scenario> sits beside <root>/raw/<scenario>."""
    return Path(proc).parent.parent / "raw" / Path(proc).name


def _selector(env: Env, text: str) -> str:
    """A selector the agent wrote: `source`, an `app` (any name the Resolver reads) or
    `app_uri`, and optionally `key`, `t_start`, `t_end`. Returns canonical selector JSON."""
    try:
        d = json.loads(text)
    except ValueError:
        raise ToolError("selector is not valid JSON") from None
    if not isinstance(d, dict) or d.get("source") not in sel.SOURCES:
        raise ToolError(f"selector needs a source, one of {', '.join(sel.SOURCES)}")
    unknown = set(d) - {"source", "app", "app_uri", "key", "t_start", "t_end"}
    if unknown:
        raise ToolError(f"unknown selector fields {sorted(unknown)}")
    app = app_uri(_app_id(env, d["app"])) if d.get("app") else d.get("app_uri")
    t0 = _when(d["t_start"]) if d.get("t_start") else datetime(2000, 1, 1, tzinfo=UTC)
    t1 = _when(d["t_end"]) if d.get("t_end") else env.as_of
    return sel.make(d["source"], app, d.get("key"), t0, t1)


def _node_evidence(env: Env, u: str) -> tuple[list[str], list[str]]:
    """(selector JSONs, raw ids) a node cites; a node that cites none borrows those of what
    it was derived from or cites, up to two steps away."""
    if not _exists(env, u):
        raise ToolError(f"{u} is not in the graph (misspelled, or not observable at the cutoff)")
    selectors: set[str] = set()
    ids: set[str] = set()
    seen, level = {u}, [u]
    for _ in range(3):
        for n in level:
            for r in _select(env, f"SELECT ?v WHERE {{ <{n}> ops:rawSelector ?v }} LIMIT {LIMIT}"):
                (selectors if r["v"].startswith("{") else ids).add(r["v"])
        if selectors or ids:
            break
        nxt = []
        for n in level[:20]:
            for r in _select(env, f"""SELECT DISTINCT ?x WHERE {{ {{ <{n}> prov:wasDerivedFrom ?x }}
                UNION {{ <{n}> ops:cites ?x }} FILTER(isIRI(?x)) }} LIMIT {LIMIT}"""):
                if r["x"] not in seen:
                    seen.add(r["x"])
                    nxt.append(r["x"])
        level = nxt
    return sorted(selectors), sorted(ids)


def _observable(env: Env, ids: list[str]) -> dict[str, datetime | None]:
    """The ids whose every parsed row is stamped before the cutoff, with the time of the first.
    A row with no time cannot be shown to precede it (catalog rows are reference data)."""
    by_source: dict[str, list[str]] = defaultdict(list)
    for i in ids:
        by_source[i.split("|", 1)[0]].append(i)
    cut = env.as_of.isoformat()
    out: dict[str, datetime | None] = {}
    con = duckdb.connect()
    try:
        for source, group in by_source.items():
            union = " UNION ALL ".join(
                f"SELECT raw_id, ts_utc FROM read_parquet('{(env.proc / f'{t}.parquet').as_posix()}')"
                for t in _SOURCE_TABLES.get(source, TABLES))
            con.register("ids", pa.table({"raw_id": group}))
            seen = con.sql(f"""SELECT t.raw_id AS raw_id, MIN(t.ts_utc) AS first,
                (COUNT(*) = COUNT(t.ts_utc) AND MAX(t.ts_utc) < TIMESTAMPTZ '{cut}') AS ok,
                COUNT(t.ts_utc) = 0 AS untimed
                FROM ({union}) t JOIN ids ON t.raw_id = ids.raw_id GROUP BY t.raw_id"""
                               ).to_arrow_table().to_pylist()
            con.unregister("ids")
            for r in seen:
                if r["ok"] or (source == "catalog" and r["untimed"]):
                    out[r["raw_id"]] = r["first"]
    finally:
        con.close()
    return out


def _read_line(raw: Path, rid: str) -> bytes | None:
    span = RawSpan.parse(rid)
    try:
        with open(raw / span.rel_path, "rb") as f:
            f.seek(span.offset)
            payload = f.read(span.length)
    except OSError:
        return None
    return payload if hashlib.sha256(payload).hexdigest()[:16] == rid.rsplit("|", 1)[1] else None


@_guarded
def raw_evidence(env: Env, target: str, limit: int) -> str:
    t = target.strip()
    limit = max(1, min(int(limit), 100))
    if not env.raw.is_dir():
        raise ToolError(f"the raw tree is not at {env.raw}; make the tools with raw_root=")
    selectors: list[str] = []
    ids: list[str] = []
    if t.startswith("{"):
        selectors = [_selector(env, t)]
    elif t.startswith(BASE + "/"):
        selectors, ids = _node_evidence(env, t)
    elif _RAW_ID.fullmatch(t):
        ids = [t]
    else:
        raise ToolError("give a graph node URI, a raw id, or a selector JSON "
                        '{"source": "logs|metrics|spans", "app": ..., "key": ..., '
                        '"t_start": ..., "t_end": ...}')
    clipped = []
    for s in selectors:
        x = sel.Selector.from_json(s)
        clipped.append(sel.Selector(x.source, x.app_uri, x.key, x.t_start,
                                    min(x.t_end, env.as_of)).to_json())
    found = sel.earliest_raw_ids(env.proc, clipped, 3 * limit, env.as_of) if clipped else []
    seen = _observable(env, sorted({*ids, *(r for r, _ in found)}))
    order = sorted(seen, key=lambda r: (seen[r] or datetime.min.replace(tzinfo=UTC), r))[:limit]
    out = []
    for rid in order:
        payload = _read_line(env.raw, rid)
        if payload is None:
            out.append({"raw_id": rid, "error": "the raw bytes do not match the id"})
            continue
        line = payload.decode("utf-8", "replace")
        row = {"raw_id": rid, "ts": seen[rid] and _z(seen[rid]), "line": _clip(line, LINE_MAX)}
        if len(line) > LINE_MAX:
            row["len"] = len(line)
        out.append(row)
    return _fit(out)


# --- findings ----------------------------------------------------------------------------

@functools.cache
def _ontology() -> Graph:
    return load_ontology()


@functools.cache
def _shapes() -> Graph:
    return load_shapes()


# rdflib's SPARQL grammar, which pyshacl parses its shapes with, breaks when first used from
# several threads at once, and the specialists record findings in parallel.
_SHACL = threading.Lock()


def _conforms(triples: list[tuple]) -> tuple[bool, str]:
    with _SHACL:
        data = Graph()
        for prefix, ns in _ontology().namespaces():
            data.bind(prefix, ns)
        data += _ontology()
        for t in triples:
            data.add(t)
        ok, _, text = pyshacl.validate(data, shacl_graph=_shapes(), inference="none",
                                       advanced=True, abort_on_first=False, allow_warnings=False)
    return bool(ok), str(text)


def _findings_graph(env: Env) -> URIRef:
    return graph_uri("findings", f"{env.scenario}/{env.specialist}")


@_guarded
def record_finding(env: Env, f: FindingIn) -> str:
    if not f.claim.strip():
        raise ToolError("a finding needs a claim")
    app = _app_id(env, f.about_app)
    missing = [u for u in f.signal_uris if not _exists(env, u)]
    if missing:
        raise ToolError(f"evidence not in the graph (misspelled, or not observable at the "
                        f"cutoff): {', '.join(missing)}")
    cites = sorted(set(f.signal_uris))
    u = node("ops", "finding", env.scenario, env.specialist, app, f.claim, *cites)
    try:
        confidence = dec(f.confidence)
    except ValueError:
        raise ToolError("confidence must be a finite number") from None
    triples = [(u, RDF.type, OPS.Finding), (u, OPS.onApplication, URIRef(app_uri(app))),
               (u, RDFS.comment, RdfLiteral(f.claim)), (u, OPS.confidence, confidence),
               *[(u, OPS.cites, URIRef(c)) for c in cites],
               *[(u, PROV.wasDerivedFrom, URIRef(c)) for c in cites]]
    context = [(URIRef(app_uri(app)), RDF.type, OPS.Application),
               (URIRef(app_uri(app)), OPS.inCatalog, RdfLiteral(True))]
    ok, text = _conforms(triples + context)
    if not ok:
        kept = "\n".join(x for x in text.splitlines() if not x.lstrip().startswith("Source Shape"))
        return f"error: SHACL rejected the finding, nothing was recorded.\n{_clip(kept, 1500)}"
    graph = _findings_graph(env)
    env.store.add_to_graph(graph, triples)
    return _dump({"uri": str(u), "graph": str(graph)})


@_guarded
def read_findings(env: Env, app: str | None) -> str:
    only = f"VALUES ?app {{ <{app_uri(_app_id(env, app))}> }}" if app else ""
    prefix = f"{BASE}/graph/findings/{env.scenario}/"
    rows = _select(env, f"""SELECT ?f ?g ?app ?claim ?conf
        (GROUP_CONCAT(DISTINCT STR(?cite); SEPARATOR=" ") AS ?cites) WHERE {{
        GRAPH ?g {{ ?f a ops:Finding ; ops:onApplication ?app ; ops:confidence ?conf .
                    OPTIONAL {{ ?f rdfs:comment ?claim }} OPTIONAL {{ ?f ops:cites ?cite }} }}
        {only} FILTER(STRSTARTS(STR(?g), "{prefix}")) }}
        GROUP BY ?f ?g ?app ?claim ?conf ORDER BY ?g ?f LIMIT {LIMIT}""")
    out = [{"uri": r["f"], "specialist": r["g"][len(prefix):], "app": _app_of(r["app"]),
            "claim": _clip(r["claim"], 300), "conf": _num(r["conf"], 3),
            "cites": sorted((r["cites"] or "").split())} for r in rows]
    return _fit(out, capped=len(rows) >= LIMIT)


# --- the toolset -------------------------------------------------------------------------

def make_tools(store: Store, proc_root: Path, scenario: str, specialist: str, *, llm=None,
               as_of: datetime | None = None, raw_root: Path | None = None) -> dict[str, BaseTool]:
    """The agent's tools over `store`, an as-of store. `as_of` is the cutoff `store` was
    harvested at (default: the live incident's, `asof.live_cutoff`); `raw_evidence` enforces
    it. `raw_root` defaults to the raw tree beside `proc_root`. `llm` lets the Resolver read
    nicknames such as "TMS"."""
    if not _SPECIALIST.fullmatch(specialist):
        raise ValueError(f"specialist {specialist!r}: letters, digits, '_' and '-' only")
    proc = Path(proc_root)
    resolver = Resolver(catalog_from_table(pl.read_parquet(proc / "catalog.parquet")), llm)
    env = Env(store=store, proc=proc, raw=Path(raw_root) if raw_root else _raw_root(proc),
              scenario=scenario, specialist=specialist, resolver=resolver,
              as_of=as_of or asof.live_cutoff(proc).as_of,
              symptom=symptom_uri(scenario, asof.live_symptom(proc)))

    @tool("find_symptom", parse_docstring=True)
    def find_symptom_tool(alert_id: str) -> str:
        """Find the alert that started the investigation, the incident it opened, and the URI
        to explain. Returns {symptom_uri, alert{uri,id,app,at,severity,metric,summary},
        incident{uri,id,at,severity,affects}|null, as_of}; the symptom is the incident when one
        was opened before the cutoff, else the alert.

        Args:
            alert_id: Radar alert id such as radar-09000, or an incident id such as INC-5377.
        """
        return find_symptom(env, alert_id)

    @tool("dependency_closure", parse_docstring=True)
    def dependency_closure_tool(app: str, direction: Literal["down", "up"]) -> str:
        """Every application an app reaches. Returns [{id,hops,kind,tier}] from the app (hops 0)
        outward. down: what the app calls, transitively. up: what calls it, transitively; an
        effect travels up the callers.

        Args:
            app: Application id or a name for it (e.g. TMS).
            direction: down for dependencies, up for callers.
        """
        return dependency_closure(env, app, direction)

    @tool("changes_in_window", parse_docstring=True)
    def changes_in_window_tool(apps: list[str], start: str, end: str) -> str:
        """Deployments, config changes, scaling events, experiment allocations and commits made
        to the apps in a time window. Returns [{id,uri,kind,app,at,what}] in time order.

        Args:
            apps: Applications (ids or names); an empty list means every application.
            start: Window start, ISO 8601 (2026-10-14T21:40Z).
            end: Window end, ISO 8601.
        """
        return changes_in_window(env, apps, start, end)

    @tool("anomalies_in_window", parse_docstring=True)
    def anomalies_in_window_tool(apps: list[str], start: str, end: str) -> str:
        """Metric anomalies whose onset is in the window, folded per app, metric and direction.
        Returns [{uri,app,metric,dir,at,mag,series}]: uri is the earliest series' anomaly,
        mag the largest magnitude in robust sigmas, series how many series fired.

        Args:
            apps: Applications (ids or names); an empty list means every application.
            start: Window start, ISO 8601.
            end: Window end, ISO 8601.
        """
        return anomalies_in_window(env, apps, start, end)

    @tool("log_spikes", parse_docstring=True)
    def log_spikes_tool(apps: list[str], start: str, end: str) -> str:
        """Log patterns that spiked over their baseline, starting in the window. Returns
        [{uri,app,at,n,z,cat,tpl}]: n lines, z robust sigmas, cat the error category,
        tpl the mined message template.

        Args:
            apps: Applications (ids or names); an empty list means every application.
            start: Window start, ISO 8601.
            end: Window end, ISO 8601.
        """
        return log_spikes(env, apps, start, end)

    @tool("trace_edges", parse_docstring=True)
    def trace_edges_tool(apps: list[str], start: str, end: str) -> str:
        """Caller-to-callee call edges that turned slow or failing, starting in the window,
        for edges touching the apps. Returns [{uri,caller,callee,type,at,n,mag,p50,p99,err}]:
        type slow_trace (mag is the p50 ratio over baseline) or failed_trace (mag in sigmas),
        p50 and p99 in ms, err the error rate.

        Args:
            apps: Applications (ids or names) at either end of the edge; empty means all.
            start: Window start, ISO 8601.
            end: Window end, ISO 8601.
        """
        return trace_edges(env, apps, start, end)

    @tool("alerts_incidents", parse_docstring=True)
    def alerts_incidents_tool(start: str, end: str) -> str:
        """Alerts that fired and incidents opened in the window, mixed in time order. Returns
        [{kind:alert,uri,id,app,at} | {kind:incident,uri,id,at,severity,affects,resolved}].

        Args:
            start: Window start, ISO 8601.
            end: Window end, ISO 8601.
        """
        return alerts_incidents(env, start, end)

    @tool("similar_incidents", parse_docstring=True)
    def similar_incidents_tool(app: str, kinds: list[str]) -> str:
        """Past resolved incidents caused by a change to the app, and how often such changes
        caused incidents of that severity. Returns {patterns[{kind,severity,support,freq}],
        incidents[{id,uri,at,severity,cause,by,affects,like_live}]}; like_live marks incidents
        the history enricher judged similar to the live incident as it was known at the cutoff.

        Args:
            app: Application whose change caused the past incident (id or name).
            kinds: Change kinds to keep (deployment, config_change, scaling,
                experiment_allocation, commit, client_release); empty keeps all.
        """
        return similar_incidents(env, app, kinds)

    @tool("chat_claims", parse_docstring=True)
    def chat_claims_tool(channel: str, kinds: list[str]) -> str:
        """What people said in an incident channel, as typed claims, oldest first. Returns
        [{uri,kind,app,at,by,quote,retracts}]; retracts is the claim a walk-back withdraws.
        Chat holds hypotheses and observations, so weigh them against telemetry.

        Args:
            channel: Slack channel such as inc-5377-homepage-ttr, or an incident id.
            kinds: Claim kinds to keep (observation, hypothesis, mitigation, root_cause,
                retraction); empty keeps all.
        """
        return chat_claims(env, channel, kinds)

    @tool("causal_chains", parse_docstring=True)
    def causal_chains_tool(symptom_uri: str) -> str:
        """The correlation engine's ranked causal chains for a symptom, best first. Returns
        [{conf,links[[signal_type,app,at,uri],...]}], each from a root change down to the
        symptom, over topology-gated, time-ordered signals. Chains under 0.6 are dropped.

        Args:
            symptom_uri: URI of the incident or alert to explain (find_symptom gives it).
        """
        return causal_chains(env, symptom_uri)

    @tool("refutation_check", parse_docstring=True)
    def refutation_check_tool(alert_id: str, candidate_uris: list[str]) -> str:
        """Test candidate root-cause changes against the graph. The symptom is the alert's
        application degrading, from the first telemetry of its episode (onset) to the alert.
        Candidates are every change (autoscaling aside) in the hour before the onset on an app
        the alert app depends on, a client that reaches it, or an app the incident was filed
        against, plus the changes you name. Each must pass: closure (the alert app depends on its app),
        timing (it landed before the onset, skew-corrected time), clients (every client of
        the alert app depends on its app), already_degrading (neither its app nor anything
        it depends on was degrading before it landed), origin (not autoscaling, not a commit:
        its deployment is the change), propagation (slow or failed calls, from it on, lead
        from its app, or a datastore's or cache's callers, up to the alert app). Returns
        {symptom{alert,app,alert_at,onset,onset_uri,clients,filed_against,filed_in_closure},
        candidates[{uri,kind,app,at,survives,refuted_by,supported_by,chain_conf}]},
        survivors first.

        Args:
            alert_id: The alert (radar-...) or incident (INC-...) under investigation.
            candidate_uris: Change URIs to test besides the ones found automatically.
        """
        return refutation_check(env, alert_id, candidate_uris)

    @tool("raw_evidence", parse_docstring=True)
    def raw_evidence_tool(uri_or_selector: str, limit: int = 20) -> str:
        """The original raw lines behind a node or a selector, earliest first, each with its
        raw_id. Returns [{raw_id,ts,line}]; a long line is cut (len gives its size). Only
        lines observable at the cutoff are returned. Accepts a graph node URI, a raw id, or a
        selector JSON {"source": "logs"|"metrics"|"spans", "app": "...", "key": "...",
        "t_start": "...", "t_end": "..."} (key and times optional).

        Args:
            uri_or_selector: Node URI, raw id, or selector JSON.
            limit: Most lines to return (1 to 100).
        """
        return raw_evidence(env, uri_or_selector, limit)

    @tool("sparql_select", parse_docstring=True)
    def sparql_select_tool(query: str) -> str:
        """Run a read-only SPARQL SELECT over the graph. Only SELECT is allowed; LIMIT 200 is
        enforced. The prefixes ops chg exp met log trc alrt inc chat app team infra prov skos
        rdf rdfs xsd are predeclared. Returns [{var: value}].

        Args:
            query: A SPARQL SELECT query.
        """
        return sparql_select(env, query)

    @tool("record_finding", parse_docstring=True)
    def record_finding_tool(f: FindingIn) -> str:
        """Record a conclusion about one application as an ops:Finding. It must cite at least
        one node already in the graph (signal_uris) and carry a confidence in [0, 1]; it is
        checked against the graph's SHACL shapes before it is stored, and the error text comes
        back if it fails. Returns {uri, graph}.

        Args:
            f: The finding: about_app, claim, signal_uris, confidence.
        """
        return record_finding(env, f if isinstance(f, FindingIn) else FindingIn(**f))

    @tool("read_findings", parse_docstring=True)
    def read_findings_tool(app: str | None = None) -> str:
        """Findings recorded by any specialist. Returns [{uri,specialist,app,claim,conf,cites}].

        Args:
            app: Only findings about this application (id or name); omit for all.
        """
        return read_findings(env, app)

    named = {"find_symptom": find_symptom_tool, "dependency_closure": dependency_closure_tool,
             "changes_in_window": changes_in_window_tool,
             "anomalies_in_window": anomalies_in_window_tool, "log_spikes": log_spikes_tool,
             "trace_edges": trace_edges_tool, "alerts_incidents": alerts_incidents_tool,
             "similar_incidents": similar_incidents_tool, "chat_claims": chat_claims_tool,
             "causal_chains": causal_chains_tool, "refutation_check": refutation_check_tool,
             "raw_evidence": raw_evidence_tool,
             "sparql_select": sparql_select_tool, "record_finding": record_finding_tool,
             "read_findings": read_findings_tool}
    return named


__all__ = ["CAP", "make_tools"]
