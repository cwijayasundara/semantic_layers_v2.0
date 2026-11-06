"""Competency questions: the runner, the expected files, and the CQs
themselves against the reference ABox. The real-store test waits for Task 12 (R8)."""
import importlib.util
import json

import pyoxigraph as ox
import pytest
from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD

from e2ekg.graph.shacl import CQ_DIR, cq_question, cq_requires, expected, run_cq, validate
from e2ekg.graph.uris import NS, app_uri, change_uri

OPS = NS["ops"]
CQS = sorted(CQ_DIR.glob("*.rq"))


def _derive_module():
    spec = importlib.util.spec_from_file_location("_derive_expected", CQ_DIR / "_derive_expected.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skip(reason="store built in Task 12")
@pytest.mark.parametrize("cq", CQS, ids=lambda p: p.stem)
def test_cq(cq, harvested_hero_store):          # fixture built in Task 12
    assert run_cq(harvested_hero_store, cq) == expected(cq)


# --- the suite itself ---------------------------------------------------------

def test_fifteen_cqs_each_with_question_and_expected_file():
    assert len(CQS) == 15
    assert [p.name[:3] for p in CQS] == [f"{i:02d}-" for i in range(1, 16)]
    for p in CQS:
        assert p.read_text().startswith("# "), p.name
        assert cq_question(p).endswith("?"), p.name
        assert p.with_suffix(".expected.json").exists(), p.name
        assert expected(p), f"{p.name} expects no rows"


def test_cq01_is_the_posts_question():
    assert cq_question(CQS[0]) == ("Which incidents affected applications owned by "
                                   "Team Bedrock in us-east-1?")


def test_expected_files_are_reproducible_from_the_answer_key():
    derived = _derive_module().derive()
    assert set(derived) == {p.stem for p in CQS}
    for p in CQS:
        on_disk = json.loads(p.with_suffix(".expected.json").read_text())
        assert on_disk == derived[p.stem], p.name


def test_expected_vars_match_select_vars():
    empty = ox.Store()
    for p in CQS:
        res = empty.query(p.read_text())
        assert [v.value for v in res.variables] == json.loads(
            p.with_suffix(".expected.json").read_text())["vars"], p.name


def test_no_cq_projects_hash_derived_nodes():
    for p in CQS:
        for row in expected(p):
            for _, v in row:
                if isinstance(v, str) and v.startswith("<"):
                    assert v.startswith(("<https://kg.local/app/application/",
                                         "<https://kg.local/chg/")), (p.name, v)


def test_spot_values_from_answer_key():
    by = {p.stem[:2]: p for p in CQS}
    rows = {k: [dict(r) for r in expected(p)] for k, p in by.items()}
    assert rows["04"] == [{"handle": "priya.nair", "pastApp": f"<{app_uri('lolomo-svc')}>"}]
    assert rows["13"] == [{"previous": f"<{app_uri('api-gateway')}>",
                           "current": f"<{app_uri('homepage-graphql')}>"}]
    assert rows["14"] == [{"support": "10", "frequency": "0.4"}]
    assert {r["app"] for r in rows["06"]} == {f"<{app_uri(a)}>" for a in
                                              ("api-gateway", "client-android", "row-ranker")}
    chg02 = {r["change"] for r in rows["02"]}
    assert f"<{change_uri('hero', 'config_change', 'chg-cfg-0001')}>" in chg02
    assert f"<{change_uri('hero', 'client_release', 'chg-rel-0001')}>" not in chg02
    assert f"<{change_uri('hero', 'deployment', 'chg-dep-0001')}>" not in chg02


def test_requires_header():
    assert cq_requires(CQS[6]) == {"correlation"}
    assert all(not cq_requires(p) for i, p in enumerate(CQS) if i != 6)


@pytest.mark.parametrize("cq", CQS, ids=lambda p: p.stem)
def test_cq_on_reference_abox(cq, hero_reference):
    assert run_cq(hero_reference, cq) == expected(cq)


def test_reference_abox_is_shacl_clean(hero_reference):
    ok, report = validate(hero_reference)
    assert ok, report


# --- the runner on small hand-built fixtures ---------------------------------

_Q = """# Which apps changed?
PREFIX ops: <https://kg.local/ops#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?app ?t ?n WHERE {
  ?c a/rdfs:subClassOf* ops:Change ; ops:changedApp ?app ; ops:atTime ?t .
  OPTIONAL { ?c ops:count ?n }
}
"""


def _fixture_triples():
    c1 = URIRef(change_uri("hero", "deployment", "d1"))
    c2 = URIRef(change_uri("hero", "scaling", "s1"))
    return [
        (c1, RDF.type, OPS.Deployment), (c1, OPS.changedApp, app_uri("a")),
        (c1, OPS.atTime, Literal("2026-10-14T15:00:00-07:00", datatype=XSD.dateTime)),
        (c1, OPS["count"], Literal(3)),
        (c2, RDF.type, OPS.ScalingEvent), (c2, OPS.changedApp, app_uri("b")),
        (c2, OPS.atTime, Literal("2026-10-14T22:30:00Z", datatype=XSD.dateTime)),
    ]


def _write(tmp_path, rows):
    q = tmp_path / "99-x.rq"
    q.write_text(_Q)
    q.with_suffix(".expected.json").write_text(json.dumps(
        {"question": "Which apps changed?", "vars": ["app", "t", "n"], "rows": rows}))
    return q


_ROWS = [{"app": f"<{app_uri('a')}>", "t": "2026-10-14T22:00:00Z", "n": "3"},
         {"app": f"<{app_uri('b')}>", "t": "2026-10-14T22:30:00Z", "n": None}]


def test_runner_on_rdflib_graph_uses_tbox_subclasses(tmp_path):
    g = Graph()
    for t in _fixture_triples():
        g.add(t)
    q = _write(tmp_path, _ROWS)
    assert run_cq(g, q) == expected(q)


def test_runner_on_rdflib_dataset(tmp_path):
    ds = Dataset()
    gr = ds.graph(URIRef("https://kg.local/graph/changes/hero"))
    for t in _fixture_triples():
        gr.add(t)
    q = _write(tmp_path, _ROWS)
    assert run_cq(ds, q) == expected(q)


def test_runner_on_oxigraph_store_with_named_graphs(tmp_path):
    st = ox.Store()
    gname = ox.NamedNode("https://kg.local/graph/changes/hero")

    def term(x):
        if isinstance(x, Literal):
            return ox.Literal(str(x), datatype=ox.NamedNode(str(x.datatype)))
        return ox.NamedNode(str(x))
    for s, p, o in _fixture_triples():
        st.add(ox.Quad(term(s), term(p), term(o), gname))
    q = _write(tmp_path, _ROWS)
    assert run_cq(st, q) == expected(q)
    assert run_cq(st, q) == expected(q)        # rerun is stable (ontology loaded once)


def test_runner_on_wrapper_holding_a_store(tmp_path):
    class Wrapper:
        def __init__(self):
            self._store = ox.Store()
    w = Wrapper()
    q = _write(tmp_path, [])
    assert run_cq(w, q) == frozenset() == expected(q)


def test_runner_detects_a_wrong_answer(tmp_path):
    g = Graph()
    for t in _fixture_triples():
        g.add(t)
    q = _write(tmp_path, _ROWS[:1])
    assert run_cq(g, q) != expected(q)
