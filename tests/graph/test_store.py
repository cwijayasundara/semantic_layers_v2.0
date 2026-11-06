"""The Oxigraph store wrapper: named-graph replacement, queries, a sorted Turtle dump."""
import random

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS

from e2ekg.graph.store import Store, graph_uri
from e2ekg.graph.uris import NS, app_uri

OPS = NS["ops"]
G1 = URIRef("https://kg.local/graph/catalog/hero")
G2 = URIRef("https://kg.local/graph/changes/hero")
# Not rdflib.namespace.XSD: pytest probes module attributes and XSD warns on unknown names.
DATETIME = URIRef("http://www.w3.org/2001/XMLSchema#dateTime")


def _triples(n: int = 5):
    out = []
    for i in range(n):
        a = app_uri(f"app-{i}")
        out += [(a, RDF.type, OPS.Application), (a, OPS.inCatalog, Literal(True)),
                (a, RDFS.label, Literal(f'app "{i}"\nline two')),
                (a, OPS.tier, Literal(i)),
                (a, OPS.atTime, Literal("2026-10-14T23:00:30.986921Z", datatype=DATETIME))]
    return out


def test_graph_uri_names_enricher_and_scenario():
    assert graph_uri("chat", "hero") == URIRef("https://kg.local/graph/chat/hero")


def test_add_to_graph_keeps_what_the_graph_holds():
    st = Store(None)
    st.replace_graph(G1, _triples(2))
    assert st.add_to_graph(G1, _triples(3)) == 15           # the 10 already held collapse
    assert st.quad_count() == 15
    assert st.add_to_graph(G2, _triples(1)) == 5
    assert st.quad_count() == 20


def test_replace_graph_counts_and_replaces(tmp_path):
    st = Store(tmp_path / "s")
    assert st.replace_graph(G1, _triples(3)) == 15
    assert st.replace_graph(G2, _triples(1)) == 5
    assert st.quad_count() == 20
    assert st.replace_graph(G1, _triples(2)) == 10          # replaced, not appended
    assert st.quad_count() == 15
    assert st.replace_graph(G1, _triples(2) + _triples(2)) == 10   # duplicates collapse
    assert st.replace_graph(G1, []) == 0
    assert st.quad_count() == 5


def test_query_runs_over_the_union_of_named_graphs():
    st = Store(None)
    st.replace_graph(G1, _triples(2))
    st.replace_graph(G2, [(app_uri("app-0"), OPS.appKind, Literal("edge"))])
    rows = st.query("PREFIX ops: <https://kg.local/ops#> SELECT ?a ?k ?t WHERE "
                    "{ ?a ops:tier ?t OPTIONAL { ?a ops:appKind ?k } } ORDER BY ?t")
    assert rows == [{"a": str(app_uri("app-0")), "k": "edge", "t": "0"},
                    {"a": str(app_uri("app-1")), "k": None, "t": "1"}]


def test_dump_turtle_is_sorted_and_byte_identical_for_any_insert_order(tmp_path):
    ts = _triples(6)
    a, b = Store(None), Store(None)
    a.replace_graph(G1, ts[:12])
    a.replace_graph(G2, ts[12:])
    shuffled = ts[:]
    random.Random(3).shuffle(shuffled)
    b.replace_graph(G2, shuffled[:7])
    b.replace_graph(G1, shuffled[7:])
    a.dump_turtle(tmp_path / "a.ttl")
    b.dump_turtle(tmp_path / "b.ttl")
    assert (tmp_path / "a.ttl").read_bytes() == (tmp_path / "b.ttl").read_bytes()
    g = Graph().parse(tmp_path / "a.ttl", format="turtle")
    assert set(g) == set(ts)                                # lossless, escapes included
    text = (tmp_path / "a.ttl").read_text()
    subjects = [ln for ln in text.splitlines() if ln.startswith("<")]
    assert subjects == sorted(subjects)


def test_dump_turtle_leaves_out_the_ontology_graph(tmp_path):
    from e2ekg.graph.uris import ONTOLOGY_GRAPH
    st = Store(None)
    st.replace_graph(ONTOLOGY_GRAPH, [(OPS.Application, RDF.type, OPS.Thing)])
    st.replace_graph(G1, _triples(1))
    st.dump_turtle(tmp_path / "x.ttl")
    assert len(Graph().parse(tmp_path / "x.ttl", format="turtle")) == 5


def test_store_persists_on_disk(tmp_path):
    st = Store(tmp_path / "s")
    st.replace_graph(G1, _triples(1))
    st.flush()
    assert any((tmp_path / "s").iterdir())
