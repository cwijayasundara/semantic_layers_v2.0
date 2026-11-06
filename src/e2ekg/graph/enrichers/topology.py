"""Topology enricher: ops:dependsOn, merged from catalog dependencies and trace edges.

Sources:
  * catalog `dependencies` (the only source of datastore and cache edges: the
    trace edge fingerprints are cross-service only), resolved to catalog ids
    through the identity resolver; a name that does not resolve is not an
    application and gives no edge;
  * cross-application trace edges (`derived/fp_edges`, caller != callee, both
    resolved), weighted by call volume (span count over the window).

Cap: at most 50 edges per node per direction (global constraint). Edges are
ranked by call volume (descending; catalog-only edges have volume 0), ties by
(caller, callee), and accepted greedily while both endpoints are under the cap.
Every dropped edge counts against both endpoints, recorded as
`ops:droppedEdges n` on the application (only when n > 0)."""
from collections import Counter

import polars as pl

from e2ekg.graph.enricher import NS, Enricher, integer, json_fields, read_table
from e2ekg.graph.uris import app_uri
from e2ekg.preprocess.identity import APP_URI_PREFIX

OPS = NS["ops"]
CAP = 50


def merge_edges(catalog: set[tuple[str, str]], traces: dict[tuple[str, str], int],
                cap: int = CAP) -> tuple[list[tuple[str, str]], dict[str, int]]:
    edges = {e: 0 for e in catalog if e[0] != e[1]}
    for e, n in traces.items():
        if e[0] != e[1]:
            edges[e] = edges.get(e, 0) + int(n)
    out, inn = Counter(), Counter()
    kept, dropped = [], Counter()
    for (a, b), _ in sorted(edges.items(), key=lambda kv: (-kv[1], kv[0])):
        if out[a] < cap and inn[b] < cap:
            kept.append((a, b))
            out[a] += 1
            inn[b] += 1
        else:
            dropped[a] += 1
            dropped[b] += 1
    return sorted(kept), dict(sorted(dropped.items()))


def _app_id(u: str | None) -> str | None:
    return u[len(APP_URI_PREFIX):] if u and u.startswith(APP_URI_PREFIX) else None


class TopologyEnricher(Enricher):
    name = "topology"
    reads = ("catalog", "derived/fp_edges")

    def fetch(self, ctx):
        cat = read_table(ctx, "catalog", ["raw_id", "fields"]).sort("raw_id")
        apps, catalog = set(), set()
        deps: list[tuple[str, str]] = []
        for f in cat["fields"]:
            d = json_fields(f)
            if not d.get("id"):
                continue
            apps.add(d["id"])
            for dep in d.get("dependencies") or ():
                name = dep.get("app") if isinstance(dep, dict) else dep
                if isinstance(name, str) and name:
                    deps.append((d["id"], name))
        for a, name in deps:
            b = name if name in apps else ctx.resolver.resolve(name).app
            if b in apps:
                catalog.add((a, b))
        e = read_table(ctx, "derived/fp_edges", ["caller_uri", "callee_uri", "n"])
        vol = (e.filter(pl.col("caller_uri").is_not_null() & pl.col("callee_uri").is_not_null()
                        & (pl.col("caller_uri") != pl.col("callee_uri")))
               .group_by("caller_uri", "callee_uri").agg(pl.col("n").sum()))
        traces = {}
        for cu, ce, n in vol.iter_rows():
            a, b = _app_id(cu), _app_id(ce)
            if a in apps and b in apps:
                traces[(a, b)] = int(n)
        return catalog, traces

    def analyze(self, raw):
        catalog, traces = raw
        return merge_edges(catalog, traces, CAP)

    def emit(self, result):
        kept, dropped = result
        for a, b in kept:
            yield app_uri(a), OPS.dependsOn, app_uri(b)
        for a, n in dropped.items():
            yield app_uri(a), OPS.droppedEdges, integer(n)
