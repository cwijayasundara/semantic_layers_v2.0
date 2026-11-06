"""The knowledge graph store: a thin wrapper over a pyoxigraph Store.

Every enricher owns one named graph, `https://kg.local/graph/<enricher>/<scenario>`,
and writes it whole with `replace_graph` (so a rerun replaces, never appends).
The ontology (TBox, schemes, extensions) sits in its own graph so SPARQL can
follow rdfs:subClassOf. Queries run over the union of the named graphs.

`dump_turtle` writes a deterministic, sorted Turtle file of the data graphs
(the ontology graph is left out: it lives in ontology/). Subjects are sorted by
their N-Triples form, predicates likewise with rdf:type first, objects likewise,
so the same quads give byte-identical output whatever the insertion order.
There are no blank nodes in the harvest, which keeps the sort total."""
import re
from collections.abc import Iterable
from pathlib import Path

import pyoxigraph as ox
from rdflib import BNode, Literal, URIRef

from e2ekg.graph.uris import BASE, NS, ONTOLOGY_GRAPH

Triple = tuple[URIRef, URIRef, URIRef | Literal]
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_PREFIXES = {**{p: str(ns) for p, ns in NS.items()},
             "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
             "rdfs": "http://www.w3.org/2000/01/rdf-schema#"}
_LOCAL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def graph_uri(name: str, scenario: str) -> URIRef:
    return URIRef(f"{BASE}/graph/{name}/{scenario}")


def to_ox(term) -> ox.NamedNode | ox.Literal | ox.BlankNode:
    if isinstance(term, URIRef):
        return ox.NamedNode(str(term))
    if isinstance(term, Literal):
        if term.language:
            return ox.Literal(str(term), language=term.language)
        if term.datatype is not None:
            return ox.Literal(str(term), datatype=ox.NamedNode(str(term.datatype)))
        return ox.Literal(str(term))
    if isinstance(term, BNode):
        return ox.BlankNode(str(term))
    if isinstance(term, (ox.NamedNode, ox.Literal, ox.BlankNode)):
        return term
    raise TypeError(f"unsupported term {term!r}")


def _value(term) -> str | None:
    if term is None:
        return None
    return term.value


class Store:
    """`Store(path)` opens (or creates) an on-disk store; `Store(None)` is in memory."""

    def __init__(self, path: Path | None, read_only: bool = False) -> None:
        self.path = Path(path) if path is not None else None
        if self.path is None:
            self._store = ox.Store()
        elif read_only:
            self._store = ox.Store.read_only(str(self.path))
        else:
            self.path.mkdir(parents=True, exist_ok=True)
            self._store = ox.Store(str(self.path))

    @property
    def oxigraph(self) -> ox.Store:
        return self._store

    def replace_graph(self, name: URIRef, triples: Iterable[Triple]) -> int:
        """Drop graph `name` and write `triples` into it. Returns the number of
        distinct triples now in the graph."""
        g = ox.NamedNode(str(name))
        quads = {ox.Quad(to_ox(s), to_ox(p), to_ox(o), g) for s, p, o in triples}
        if self._store.contains_named_graph(g):
            self._store.remove_graph(g)
        if quads:
            self._store.extend(quads)
        else:
            self._store.add_graph(g)
        return len(quads)

    def clear_graph(self, name: URIRef) -> None:
        g = ox.NamedNode(str(name))
        if self._store.contains_named_graph(g):
            self._store.remove_graph(g)

    def query(self, sparql: str) -> list[dict[str, str | None]] | bool:
        """SELECT: one dict per solution, var -> IRI string or literal lexical form
        (None when unbound). ASK: a bool."""
        res = self._store.query(sparql, use_default_graph_as_union=True)
        if isinstance(res, ox.QueryBoolean):
            return bool(res)
        names = [v.value for v in res.variables]
        return [{n: _value(sol[n]) for n in names} for sol in res]

    def quad_count(self) -> int:
        return len(self._store)

    def data_graphs(self) -> list[ox.NamedNode]:
        onto = str(ONTOLOGY_GRAPH)
        return sorted((g for g in self._store.named_graphs() if g.value != onto),
                      key=lambda g: g.value)

    def triples(self, graphs: Iterable[ox.NamedNode] | None = None) -> set[tuple]:
        """Distinct (s, p, o) pyoxigraph terms over `graphs` (default: the data graphs)."""
        out: set[tuple] = set()
        for g in (self.data_graphs() if graphs is None else graphs):
            for q in self._store.quads_for_pattern(None, None, None, g):
                out.add((q.subject, q.predicate, q.object))
        return out

    def dump_turtle(self, out: Path) -> None:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        by_subject: dict[str, dict[str, set[str]]] = {}
        for s, p, o in self.triples():
            by_subject.setdefault(str(s), {}).setdefault(p.value, set()).add(_obj(o))
        lines = [f"@prefix {p}: <{ns}> ." for p, ns in sorted(_PREFIXES.items())]
        for s in sorted(by_subject):
            lines.append("")
            lines.append(s)
            preds = sorted(by_subject[s], key=lambda p: (p != RDF_TYPE, p))
            for i, p in enumerate(preds):
                objs = sorted(by_subject[s][p])
                sep = " ;" if i < len(preds) - 1 else " ."
                lines.append(f"    {_pred(p)} {', '.join(objs)}{sep}")
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(out)

    def flush(self) -> None:
        self._store.flush()


def _curie(iri: str) -> str | None:
    for p, ns in _PREFIXES.items():
        if iri.startswith(ns) and _LOCAL.fullmatch(iri[len(ns):]):
            return f"{p}:{iri[len(ns):]}"
    return None


def _pred(iri: str) -> str:
    if iri == RDF_TYPE:
        return "a"
    return _curie(iri) or f"<{iri}>"


def _obj(o) -> str:
    if isinstance(o, ox.NamedNode):
        return _curie(o.value) or str(o)
    return str(o)         # N-Triples literal syntax is valid Turtle


__all__ = ["Store", "Triple", "graph_uri", "to_ox"]
