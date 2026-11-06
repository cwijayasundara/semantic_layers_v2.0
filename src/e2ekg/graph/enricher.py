"""The enricher framework.

An enricher turns processed tables (and, where it says so in `reads`, graphs
written by earlier enrichers) into typed triples in its own named graph:

    fetch(ctx) -> raw        read inputs
    analyze(raw) -> result   pure computation
    emit(result) -> triples  ontology-conformant triples (core.ttl + extensions)

`run` calls the three in order and writes the triples with `Store.replace_graph`
into `https://kg.local/graph/<name>/<scenario>`, so a rerun replaces, never
appends (idempotent). Any exception becomes `EnrichmentResult(success=False)`
(best effort: the harvest goes on). A failed enricher's graph is CLEARED rather
than left stale, so the store never mixes outputs of two input versions.

Provenance (core.ttl header): a node built from raw records (changes, alerts,
pages, incidents, messages, catalog apps) carries one `ops:rawSelector` per raw
record id (source|rel_path|offset|length|sha); a fingerprint carries the JSON
selector of `e2ekg.preprocess.selector` (it starts with "{"). Nodes built from
other nodes (claims, patterns) use prov:wasDerivedFrom."""
import hashlib
import json
import re
import traceback
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
from rdflib import Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from e2ekg.graph.store import Store, Triple, graph_uri
from e2ekg.graph.uris import NS, person_uri, uri

OPS, PROV, TEAM = NS["ops"], NS["prov"], NS["team"]


@dataclass(frozen=True)
class EnrichmentResult:
    success: bool
    triples_added: int
    error: str | None


@dataclass
class HarvestContext:
    scenario: str
    proc_root: Path
    store: Store
    resolver: Any            # e2ekg.preprocess.identity.Resolver
    llm: Any                 # e2ekg.llm.LLMClient or None


class Enricher(ABC):
    name: str = ""
    reads: tuple[str, ...] = ()

    @abstractmethod
    def fetch(self, ctx: HarvestContext) -> Any: ...

    @abstractmethod
    def analyze(self, raw: Any) -> Any: ...

    @abstractmethod
    def emit(self, result: Any) -> Iterable[Triple]: ...

    def run(self, ctx: HarvestContext) -> EnrichmentResult:
        g = graph_uri(self.name, ctx.scenario)
        try:
            self.scenario = ctx.scenario
            n = ctx.store.replace_graph(g, self.emit(self.analyze(self.fetch(ctx))))
            return EnrichmentResult(success=True, triples_added=n, error=None)
        except Exception as e:             # best effort: one enricher never blocks the rest
            ctx.store.clear_graph(g)
            tb = traceback.format_exception_only(type(e), e)[-1].strip()
            return EnrichmentResult(success=False, triples_added=0, error=tb)


# --- shared fetch / emit helpers -----------------------------------------------------

def read_table(ctx: HarvestContext, name: str, columns: list[str] | None = None):
    """A processed table: `<proc>/<name>.parquet`, or `<proc>/derived/<x>.parquet`
    for `name="derived/<x>"`."""
    return pl.read_parquet(Path(ctx.proc_root) / f"{name}.parquet", columns=columns)


def json_fields(s: str | None) -> dict:
    try:
        d = json.loads(s or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def recovered(f: dict, key: str):
    """`f[key]`, or the value an evt.missing_field mess moved into the free-text
    description as "(<key>: <value>)"."""
    if f.get(key) not in (None, ""):
        return f[key]
    m = re.search(rf"\({re.escape(key)}: ([^()]*)\)", str(f.get("description") or ""))
    return m.group(1).strip() if m else None


def h(*parts: object, n: int = 16) -> str:
    """Content hash for node ids (never free text in URIs)."""
    return hashlib.sha256("\x1f".join("" if p is None else str(p) for p in parts)
                          .encode()).hexdigest()[:n]


def node(ns: str, kind: str, scenario: str, *key: object) -> URIRef:
    return uri(ns, kind, f"{scenario}/{h(*key)}")


def ts(dt: datetime) -> Literal:
    """xsd:dateTime in UTC with microseconds kept (and no trailing zeros noise)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return Literal(dt.astimezone(UTC).isoformat().replace("+00:00", "Z"), datatype=XSD.dateTime)


def dec(x: float | int | Decimal) -> Literal:
    """xsd:decimal (plain digits, never an exponent). Raises on NaN/inf."""
    d = x if isinstance(x, Decimal) else Decimal(repr(float(x)))
    if not d.is_finite():
        raise ValueError(f"not a finite decimal: {x!r}")
    if d == d.to_integral_value():
        d = d.to_integral_value()
    return Literal(format(d, "f"), datatype=XSD.decimal)


def finite(x) -> bool:
    try:
        return x is not None and Decimal(repr(float(x))).is_finite()
    except (TypeError, ValueError):
        return False


def integer(n: int) -> Literal:
    return Literal(int(n), datatype=XSD.integer)


def string(s: str) -> Literal:
    return Literal(s, datatype=XSD.string)


def person(handle: str) -> tuple[URIRef, list[Triple]]:
    p = person_uri(handle)
    return p, [(p, RDF.type, OPS.Person), (p, TEAM.handle, string(handle))]


def raw_selectors(s: URIRef, raw_ids: Iterable[str]) -> list[Triple]:
    return [(s, OPS.rawSelector, string(r)) for r in sorted(set(raw_ids)) if r]


def label(s: URIRef, text: str) -> Triple:
    return s, RDFS.label, Literal(text)


__all__ = ["Enricher", "EnrichmentResult", "HarvestContext", "dec", "h", "integer", "label",
           "node", "person", "raw_selectors", "recovered", "string", "ts"]
