"""Competency questions: the runner, the expected files, and the CQs
themselves against the reference ABox and the harvested store (Task 12, R8/R25).

The committed expected files are for the FULL-SCALE hero (R25). The default
run harvests the 0.05 fixture, so `test_cq` re-derives the expectations from
that fixture's own answers (`_derive_expected.derive(answers=...)`, written to
tmp) and compares against those. `test_cq_full_scale` (slow) compares the
full-scale harvest (`e2ekg harvest --scenario hero`, store at
data/graph/hero/store or $E2EKG_FULL_STORE) against the committed files."""
import json
import os
import re
from pathlib import Path

import pyoxigraph as ox
import pytest
from rdflib import Dataset, Graph, Literal, URIRef
from rdflib import namespace as rdfns  # not `import XSD`: pytest probes module attrs
from rdflib.namespace import RDF

from e2ekg.graph.shacl import (
    CQ_DIR, ENRICHER_NAMES, LATER_STAGES, cq_mismatch, cq_question, cq_requires, expected, run_cq,
    validate,
)
from e2ekg.graph.store import Store
from e2ekg.graph.uris import NS, app_uri, change_uri

OPS = NS["ops"]
CQS = sorted(CQ_DIR.glob("*.rq"))
ROW_CQS = [p for p in CQS if "rows" in json.loads(p.with_suffix(".expected.json").read_text())]


def _gate(cq: Path) -> None:
    later = cq_requires(cq) & LATER_STAGES
    if later:
        pytest.skip(f"{cq.stem} requires {sorted(later)} (Task 13 correlation)")


@pytest.mark.parametrize("cq", CQS, ids=lambda p: p.stem)
def test_cq(cq, harvested_hero_store, fixture_expected):
    _gate(cq)
    assert cq_mismatch(harvested_hero_store, fixture_expected / cq.name) is None


FULL_STORE = Path(os.environ.get("E2EKG_FULL_STORE",
                                  CQ_DIR.parents[1] / "data" / "graph" / "hero" / "store"))


@pytest.fixture(scope="module")
def full_scale_store():
    if not FULL_STORE.is_dir():
        pytest.skip(f"no full-scale hero harvest at {FULL_STORE}; run "
                    "`uv run e2ekg harvest --scenario hero --llm scripted` first")
    return Store(FULL_STORE, read_only=True)


@pytest.mark.slow
@pytest.mark.parametrize("cq", CQS, ids=lambda p: p.stem)
def test_cq_full_scale(cq, full_scale_store):
    _gate(cq)
    assert cq_mismatch(full_scale_store, cq) is None


def test_cq05_held_is_a_boolean_that_compares_as_the_string_false(harvested_hero_store,
                                                                   fixture_expected):
    p = fixture_expected / next(c.name for c in CQS if c.name.startswith("05-"))
    (row,) = run_cq(harvested_hero_store, p)
    assert dict(row)["held"] == "false"
    assert json.loads(p.with_suffix(".expected.json").read_text())["rows"][0]["held"] == "false"


# --- the suite itself ---------------------------------------------------------

def test_fifteen_cqs_each_with_question_and_expected_file():
    assert len(CQS) == 15
    assert [p.name[:3] for p in CQS] == [f"{i:02d}-" for i in range(1, 16)]
    for p in CQS:
        assert p.read_text().startswith("# "), p.name
        assert cq_question(p).endswith("?"), p.name
        assert p.with_suffix(".expected.json").exists(), p.name
        assert p not in ROW_CQS or expected(p), f"{p.name} expects no rows"
    assert [p.stem for p in CQS if p not in ROW_CQS] == ["07-causal-chain-owners"]   # R38


def test_cq01_is_the_posts_question():
    assert cq_question(CQS[0]) == ("Which incidents affected applications owned by "
                                   "Team Bedrock in us-east-1?")


def test_expected_files_are_reproducible_from_the_generator(derive_module):
    derived = derive_module.derive(strict=False)
    missing = {p.stem for p in CQS} - set(derived)
    assert {m[:2] for m in missing} <= derive_module.NEEDS_MANIFEST
    for p in CQS:
        if p.stem in derived:
            on_disk = json.loads(p.with_suffix(".expected.json").read_text())
            assert on_disk == derived[p.stem], p.name
    if missing:
        pytest.skip(f"generator manifest missing, not re-derived: {sorted(missing)}")


def test_derive_fails_clearly_without_the_manifest(derive_module, tmp_path, capsys):
    with pytest.raises(derive_module.GeneratorOutputMissing, match="e2ekg generate"):
        derive_module.derive(answers=tmp_path)
    assert derive_module.main(["--answers", str(tmp_path)]) == 2
    assert "e2ekg generate" in capsys.readouterr().err


def test_expected_vars_match_select_vars():
    empty = ox.Store()
    for p in CQS:
        res = empty.query(p.read_text())
        assert [v.value for v in res.variables] == json.loads(
            p.with_suffix(".expected.json").read_text())["vars"], p.name


def test_no_cq_projects_hash_derived_nodes_or_scenario_change_ids():
    for p in ROW_CQS:
        for row in expected(p):
            for _, v in row:
                if isinstance(v, str) and v.startswith("<"):
                    assert v.startswith(("<https://kg.local/app/application/",
                                         "<https://kg.local/chg/")), (p.name, v)
                    assert not re.search(r"/chg-[a-z]+-\d+>$", v), (p.name, v)


def test_change_rows_are_external_ids_of_the_answer_key(derive_module):
    ext = derive_module.EXTERNAL_OF              # scenario change id -> external id
    by = {p.stem[:2]: p for p in CQS}
    chg02 = {dict(r)["change"] for r in expected(by["02"])}

    def u(cid, kind):
        return f"<{change_uri('hero', kind, ext[cid])}>"
    assert u("chg-cfg-0001", "config_change") in chg02
    assert u("chg-exp-0001", "experiment_allocation") in chg02
    assert u("chg-rel-0001", "client_release") not in chg02
    assert u("chg-dep-0001", "deployment") not in chg02
    assert {dict(r)["change"] for r in expected(by["15"])} == {
        u("chg-exp-0001", "experiment_allocation")}


def test_spot_values_from_the_generator(derive_module):
    by = {p.stem[:2]: p for p in CQS}
    rows = {k: [dict(r) for r in expected(p)] for k, p in by.items() if p in ROW_CQS}
    assert rows["04"] == [{"handle": "priya.nair", "pastApp": f"<{app_uri('lolomo-svc')}>"}]
    assert rows["13"] == [{"incidentId": "INC-5377", "previous": f"<{app_uri('api-gateway')}>",
                           "current": f"<{app_uri('homepage-graphql')}>"}]
    assert rows["14"] == [{"support": "10", "frequency": "0.4"}]
    # CQ05: the authored "bump min to 40 on the TMS ASG" beat, 30 s after as_of; the poller undid it.
    beat = next(b for b in derive_module.live_beats() if "bump min to 40 on the TMS ASG" in b.text)
    assert rows["05"] == [{"handle": beat.handle, "at": derive_module.ts(beat.at), "held": "false"}]
    assert beat.at > derive_module.AS_OF
    # CQ09/CQ12: the real pages and alerts, noise included.
    assert {r["app"] for r in rows["09"]} == {f"<{app_uri(a)}>" for a in
                                              ("edge-gateway", "homepage-graphql")}
    assert {f"<{app_uri(a)}>" for a in ("edge-gateway", "homepage-graphql", "api-gateway",
                                        "artwork-svc")} <= {r["app"] for r in rows["12"]}
    # CQ10 counts deploy-shipped commits, which have no scenario change.
    assert {"kind": "commit", "n": "6"} in rows["10"]


def test_cq06_follows_the_generation_manifest(derive_module):
    try:
        rets = derive_module.retractions(derive_module.DEFAULT_ANSWERS)
    except derive_module.GeneratorOutputMissing as e:
        pytest.skip(str(e))
    pre = {r.app for r in rets if r.at < derive_module.AS_OF}
    post = {r.app for r in rets if r.at >= derive_module.AS_OF}
    p = next(p for p in CQS if p.name.startswith("06-"))
    assert {dict(r)["app"] for r in expected(p)} == {f"<{app_uri(a)}>" for a in pre}
    assert "api-gateway" in post - pre          # seed 7: its first retraction is after as_of


def test_as_of_literal_matches_the_generator(derive_module):
    lit = derive_module.lit(derive_module.AS_OF)
    assert lit == '"2026-10-14T23:00:00Z"^^xsd:dateTime'
    for k in ("03", "06", "09", "10", "11"):
        p = next(p for p in CQS if p.name.startswith(k))
        assert lit in p.read_text(), p.name


def test_requires_header():
    for p in CQS:
        req = cq_requires(p)
        assert req and req <= set(ENRICHER_NAMES), (p.name, req)
    gated = [p.name[:2] for p in CQS if cq_requires(p) & LATER_STAGES]
    assert gated == []


@pytest.mark.parametrize("cq", CQS, ids=lambda p: p.stem)
def test_cq_on_reference_abox(cq, hero_reference_built):
    g, has_retractions = hero_reference_built
    if cq.name.startswith("06-") and not has_retractions:
        pytest.skip("generator manifest missing: no retractions in the reference ABox")
    assert cq_mismatch(g, cq) is None


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
    c1 = URIRef(change_uri("hero", "deployment", "01JDEADBEEF"))
    c2 = URIRef(change_uri("hero", "scaling", "0edf7fa4-6f6b"))
    return [
        (c1, RDF.type, OPS.Deployment), (c1, OPS.changedApp, app_uri("a")),
        (c1, OPS.atTime, Literal("2026-10-14T15:00:00-07:00", datatype=rdfns.XSD.dateTime)),
        (c1, OPS["count"], Literal(3)),
        (c2, RDF.type, OPS.ScalingEvent), (c2, OPS.changedApp, app_uri("b")),
        (c2, OPS.atTime, Literal("2026-10-14T22:30:00Z", datatype=rdfns.XSD.dateTime)),
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


def test_cq07_rejects_a_top_chain_rooted_at_the_trap(hero_reference_built):
    g, _ = hero_reference_built
    trap = Graph()
    for t in g:
        trap.add(t)
    chains = sorted(trap.subjects(RDF.type, OPS.CausalChain),
                    key=lambda c: float(trap.value(c, OPS.confidence)))
    weak, _ = chains
    trap.set((weak, OPS.confidence, Literal(0.95)))
    cq = next(p for p in CQS if p.name.startswith("07-"))
    problems = cq_mismatch(trap, cq)
    assert cq_mismatch(g, cq) is None
    assert "top chain root" in problems and "api-gateway" in problems
