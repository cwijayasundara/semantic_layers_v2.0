"""Ontology as code: load the TBox and SKOS schemes, validate a graph against
ontology/shapes.ttl, and run the competency-question (CQ) suite.

Graph inputs (`validate`, `run_cq`) may be an rdflib Graph or Dataset, a
pyoxigraph Store, or any object holding a pyoxigraph Store as an attribute
(the Task 12 `Store` wrapper). CQs always run on Oxigraph, the production
engine, over the union of all named graphs plus the ontology (data in a
caller's default graph is not seen; harvest writes named graphs only).

A CQ file is SPARQL whose first line is `# <natural-language question>`;
its `# requires: <enricher>[, ...]` line names the enrichers that must have
run for it to be answerable (names from ENRICHER_NAMES). A CQ requiring a
LATER_STAGES enricher is gated until that stage exists. Its answer key sits beside it as `<name>.expected.json`:
`{"question", "vars", "rows": [{var: value}]}`. Values are encoded as
`"<iri>"` for IRIs, the lexical form for literals (dateTimes normalized to
UTC `...Z`, numbers canonicalized), and null for unbound. Results compare as
a set of rows.
"""
import json
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import pyoxigraph as ox
import pyshacl
from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import XSD

from e2ekg.graph.uris import NS, ONTOLOGY_GRAPH

ONTOLOGY_DIR = Path(__file__).resolve().parents[3] / "ontology"
CQ_DIR = ONTOLOGY_DIR / "cq"
SCHEMES = ("error_category", "fault_kind", "change_kind", "severity", "message_category",
           "sentiment", "claim_kind", "signal_type")

Row = tuple[tuple[str, str | None], ...]

# Enricher names a CQ may require: the ENRICHERS, then later stages (none now).
ENRICHER_NAMES = ("catalog", "topology", "changes", "metrics", "logs", "traces",
                  "alerts_incidents", "chat", "history", "correlation")
LATER_STAGES: frozenset[str] = frozenset()

_INTS = {str(XSD[t]) for t in ("integer", "int", "long", "short", "byte", "nonNegativeInteger",
                                "positiveInteger", "unsignedInt", "unsignedLong")}
_FLOATS = {str(XSD.decimal), str(XSD.double), str(XSD.float)}
_DATETIME = str(XSD.dateTime)
_BOOLEAN = str(XSD.boolean)


def _ontology_files() -> list[Path]:
    """core.ttl, the SKOS schemes, then the approved extensions (ontology/extensions/)."""
    return [ONTOLOGY_DIR / "core.ttl", *(ONTOLOGY_DIR / "schemes" / f"{s}.ttl" for s in SCHEMES),
            *sorted((ONTOLOGY_DIR / "extensions").glob("*.ttl"))]


@lru_cache(maxsize=1)
def _ontology_nt() -> str:
    return load_ontology().serialize(format="nt")


def load_ontology() -> Graph:
    """core.ttl, every SKOS scheme and the extensions, as a fresh rdflib Graph."""
    g = Graph()
    for p in _ontology_files():
        g.parse(p, format="turtle")
    for prefix, ns in NS.items():
        g.bind(prefix, ns)
    return g


def load_shapes() -> Graph:
    return Graph().parse(ONTOLOGY_DIR / "shapes.ttl", format="turtle")


# --- adapters ------------------------------------------------------------------

def _oxigraph_in(obj) -> ox.Store | None:
    if isinstance(obj, ox.Store):
        return obj
    for v in getattr(obj, "__dict__", {}).values():
        if isinstance(v, ox.Store):
            return v
    return None


def to_rdflib(obj, into: Graph | None = None) -> Graph:
    """Flatten any supported graph input into one rdflib Graph (all named graphs
    merged), added to `into` when given."""
    g = Graph() if into is None else into
    if isinstance(obj, Dataset):
        for c in obj.graphs():
            for t in c:
                g.add(t)
        return g
    if isinstance(obj, Graph):
        if into is None:
            return obj
        for t in obj:
            g.add(t)
        return g
    st = _oxigraph_in(obj)
    if st is None:
        raise TypeError(f"unsupported graph input {type(obj).__name__}")
    for name in st.named_graphs():
        g.parse(data=st.dump(format=ox.RdfFormat.N_TRIPLES, from_graph=name).decode(),
                format="nt")
    default = st.dump(format=ox.RdfFormat.N_TRIPLES, from_graph=ox.DefaultGraph()).decode()
    if default.strip():
        g.parse(data=default, format="nt")
    return g


def _to_oxigraph(obj) -> ox.Store:
    """An Oxigraph store holding the data plus the ontology in ONTOLOGY_GRAPH.
    rdflib inputs are copied into a fresh in-memory store. A caller's store
    gets the ontology graph added once if it is missing (harvest normally
    loads it itself)."""
    st = _oxigraph_in(obj)
    if st is None:
        st = ox.Store()
        nt = to_rdflib(obj).serialize(format="nt")
        if nt.strip():
            st.load(nt, format=ox.RdfFormat.N_TRIPLES,
                    to_graph=ox.NamedNode("https://kg.local/graph/data"))
    onto = ox.NamedNode(str(ONTOLOGY_GRAPH))
    if not st.contains_named_graph(onto):
        st.load(_ontology_nt(), format=ox.RdfFormat.N_TRIPLES, to_graph=onto)
    return st


# --- SHACL -------------------------------------------------------------------

def validate(graph_or_store) -> tuple[bool, str]:
    """SHACL-validate against ontology/shapes.ttl, with the ontology mixed in.
    The mix is done here on a copy: with pyshacl 0.40.2's own ont_graph
    mixing, the rdf:type rule flagged typed scheme nodes as untyped."""
    data = to_rdflib(graph_or_store, into=load_ontology())
    conforms, _, text = pyshacl.validate(
        data, shacl_graph=load_shapes(), inference="none", advanced=True,
        abort_on_first=False, allow_warnings=False)
    return bool(conforms), str(text)


# --- competency questions ----------------------------------------------------

def _header(path: Path) -> list[str]:
    out = []
    for line in Path(path).read_text().splitlines():
        if not line.startswith("#"):
            break
        out.append(line[1:].strip())
    return out


def cq_question(path: Path) -> str:
    return _header(path)[0]


def cq_requires(path: Path) -> set[str]:
    for line in _header(path)[1:]:
        if line.startswith("requires:"):
            return {x.strip() for x in line.removeprefix("requires:").split(",") if x.strip()}
    return set()


def _num(v) -> str:
    if isinstance(v, int) or (isinstance(v, Decimal) and v == v.to_integral_value()):
        return str(int(v))
    return format(float(v), ".12g")


def _literal(lex: str, datatype: str | None) -> str:
    if datatype == _DATETIME:
        dt = datetime.fromisoformat(lex)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if datatype == _BOOLEAN:
        return "true" if lex.strip() in ("true", "1") else "false"
    if datatype in _INTS:
        return str(int(lex))
    if datatype in _FLOATS:
        return _num(Decimal(lex)) if datatype == str(XSD.decimal) else _num(float(lex))
    return lex


def encode(term) -> str | None:
    """Canonical string for a result term (rdflib or pyoxigraph)."""
    if term is None:
        return None
    if isinstance(term, (URIRef, ox.NamedNode)):
        return f"<{term if isinstance(term, URIRef) else term.value}>"
    if isinstance(term, (BNode, ox.BlankNode)):
        return "_:"
    if isinstance(term, Literal):
        return _literal(str(term), str(term.datatype) if term.datatype else None)
    if isinstance(term, ox.Literal):
        return _literal(term.value, term.datatype.value if term.datatype else None)
    raise TypeError(f"unexpected term {term!r}")


def _row(d: dict[str, str | None]) -> Row:
    return tuple(sorted(d.items()))


def run_cq(store, path: Path) -> frozenset[Row]:
    st = _to_oxigraph(store)
    res = st.query(Path(path).read_text(), use_default_graph_as_union=True)
    names = [v.value for v in res.variables]
    return frozenset(_row({n: encode(sol[n]) for n in names}) for sol in res)


def expected(path: Path) -> frozenset[Row]:
    payload = json.loads(Path(path).with_suffix(".expected.json").read_text())
    vars_ = payload["vars"]
    return frozenset(_row({v: r.get(v) for v in vars_}) for r in payload["rows"])
