"""History enricher: learned patterns and incident similarity, over the graphs
written by alerts_incidents, changes and topology.

  * ops:Pattern per (cause change kind, cause app, severity) shared by at least
    two resolved incidents: ops:causeKind, ops:onApplication (the cause app),
    ops:severity, ops:support (matching incidents), ops:frequency (support over
    every resolved incident of that severity, those with no identified change
    included) and prov:wasDerivedFrom each matching incident. Example:
    deployments on api-gateway behind 10 of 25 SEV2s, frequency 0.4.
  * ops:similarTo (both directions) between resolved incidents with the same
    cause shape (cause kind, cause app). An unresolved incident (still open) takes
    its candidate shapes from the changes in the LOOKBACK before it opened on
    its current affected apps and their dependency closure (ops:dependsOn+):
    a past cause that recurs where this incident can be caused.
The live incident's current affected service may be a correction made after
the RCA cutoff (R21), so its similarity is a whole-window fact."""
from collections import defaultdict
from datetime import datetime, timedelta

from rdflib import Literal, URIRef
from rdflib.namespace import PROV, RDF, RDFS

from e2ekg.graph.enricher import NS, Enricher, dec, integer, node
from e2ekg.graph.uris import concept

OPS = NS["ops"]
MIN_SUPPORT = 2
LOOKBACK = timedelta(hours=2)
_PFX = """PREFIX ops: <https://kg.local/ops#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""


class HistoryEnricher(Enricher):
    name = "history"
    reads = ("graph:alerts_incidents", "graph:changes", "graph:topology")

    def fetch(self, ctx):
        q = ctx.store.query
        incidents = q(_PFX + """SELECT ?i ?t ?sev ?kind ?cause ?by WHERE {
            ?i a ops:Incident ; ops:atTime ?t .
            OPTIONAL { ?i ops:severity/skos:notation ?sev }
            OPTIONAL { ?i ops:resolvedBy ?by }
            OPTIONAL { ?i ops:causeKind/skos:notation ?kind ; ops:causeApp ?cause } }""")
        affects = q(_PFX + "SELECT ?i ?a WHERE { ?i a ops:Incident ; ops:affects ?a }")
        changes = q(_PFX + """SELECT ?kind ?app ?t WHERE {
            ?c a/rdfs:subClassOf* ops:Change ; ops:changeKind/skos:notation ?kind ;
               ops:changedApp ?app ; ops:atTime ?t }""")
        edges = q(_PFX + "SELECT ?a ?b WHERE { ?a ops:dependsOn ?b }")
        return incidents, affects, changes, edges

    def analyze(self, raw):
        incidents, affects, changes, edges = raw
        inc = {r["i"]: {**r, "t": datetime.fromisoformat(r["t"])} for r in incidents}
        aff = defaultdict(set)
        for r in affects:
            aff[r["i"]].add(r["a"])
        down = defaultdict(set)
        for r in edges:
            down[r["a"]].add(r["b"])

        resolved = {i: x for i, x in inc.items() if x["by"]}
        groups = defaultdict(list)
        for i, x in resolved.items():
            if x["kind"]:
                groups[(x["kind"], x["cause"], x["sev"])].append(i)
        by_sev = defaultdict(int)
        for x in resolved.values():
            by_sev[x["sev"]] += 1
        patterns = [{"kind": k, "app": a, "sev": s, "support": len(ids),
                     "frequency": len(ids) / by_sev[s], "incidents": sorted(ids)}
                    for (k, a, s), ids in sorted(groups.items(), key=lambda kv: str(kv[0]))
                    if s and len(ids) >= MIN_SUPPORT]

        shapes = defaultdict(set)
        for i, x in resolved.items():
            if x["kind"]:
                shapes[(x["kind"], x["cause"])].add(i)
        similar = set()
        for ids in shapes.values():
            similar |= {(a, b) for a in ids for b in ids if a != b}
        for i, x in inc.items():
            if i in resolved:
                continue
            reach = _closure(aff[i], down)
            for c in changes:
                t = datetime.fromisoformat(c["t"])
                if c["app"] in reach and x["t"] - LOOKBACK <= t <= x["t"]:
                    for j in shapes.get((c["kind"], c["app"]), ()):
                        similar |= {(i, j), (j, i)}
        return patterns, sorted(similar)

    def emit(self, result):
        patterns, similar = result
        for p in patterns:
            u = node("ops", "pattern", self.scenario, p["kind"], p["app"], p["sev"])
            yield u, RDF.type, OPS.Pattern
            yield u, RDFS.label, _label(p)
            yield u, OPS.causeKind, concept("change_kind", p["kind"])
            yield u, OPS.onApplication, URIRef(p["app"])
            yield u, OPS.severity, concept("severity", p["sev"])
            yield u, OPS.support, integer(p["support"])
            yield u, OPS.frequency, dec(round(p["frequency"], 6))
            for i in p["incidents"]:
                yield u, PROV.wasDerivedFrom, URIRef(i)
        for a, b in similar:
            yield URIRef(a), OPS.similarTo, URIRef(b)


def _closure(start: set[str], down: dict[str, set[str]]) -> set[str]:
    seen, todo = set(start), list(start)
    while todo:
        for b in down.get(todo.pop(), ()):
            if b not in seen:
                seen.add(b)
                todo.append(b)
    return seen


def _label(p: dict) -> Literal:
    return Literal(f"{p['kind']}@{p['app'].rsplit('/', 1)[-1]} {p['sev']}")
