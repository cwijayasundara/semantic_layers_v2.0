"""Ontology evolution (Task 15): mining what the ontology fails to cover, LLM proposals,
and the gate a proposal must pass. The LLM is a test-local deterministic client; no
network."""
import json
import shutil
from pathlib import Path

import polars as pl
import pytest
from rdflib import Graph, URIRef
from rdflib.namespace import SKOS

from e2ekg import cli
from e2ekg.graph.shacl import load_ontology
from e2ekg.ontology_evolve.propose import (
    Candidates, ProposalBatch, ProposalDraft, check_proposal, mine_candidates, propose,
)

NEW_ERROR_CONCEPT_TTL = """
ecat:connection_refused a skos:Concept ;
    skos:inScheme <https://kg.local/log/scheme/error_category> ;
    skos:topConceptOf <https://kg.local/log/scheme/error_category> ;
    skos:notation "connection_refused" ;
    skos:prefLabel "Connection refused"@en ;
    skos:definition "A peer actively refused a connection, typically a dependency that is not yet listening."@en .
<https://kg.local/log/scheme/error_category> skos:hasTopConcept ecat:connection_refused .
"""


def write(path: Path, ttl: str) -> Path:
    path.write_text(ttl)
    return path


@pytest.fixture(scope="module")
def candidates(preprocessed_hero, harvested_hero_store) -> Candidates:
    return mine_candidates(preprocessed_hero.proc, harvested_hero_store)


class DeterministicClient:
    """Answers each candidate kind with one concept (or property) per candidate, from the
    request JSON alone."""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def structured(self, system: str, user: str, schema):
        assert schema is ProposalBatch
        req = json.loads(user)
        self.requests.append(req)
        if req["kind"] == "tag_keys":
            body = "\n".join(
                f'met:tag_{k.replace(".", "_")} a owl:DatatypeProperty ; rdfs:domain ops:MetricSeries ;'
                f' rdfs:range xsd:string ; rdfs:label "{k}" ; rdfs:comment "Metric tag {k}." .'
                for k in (c["key"] for c in req["candidates"]))
            return ProposalBatch(proposals=[ProposalDraft(
                turtle=body, rationale="tag keys with no property", examples=[
                    c["key"] for c in req["candidates"]])])
        return ProposalBatch(proposals=[ProposalDraft(
            turtle=NEW_ERROR_CONCEPT_TTL, rationale="connection refused has no concept",
            examples=[c["template"] for c in req["candidates"]][:2])])


# --- mining -------------------------------------------------------------------

def test_candidates_include_unmapped_template(candidates):
    joined = " ".join(candidates.error_templates).lower()
    assert "connection refused" in joined
    assert "slow call" in joined


def test_templates_that_already_have_a_concept_are_not_candidates(candidates):
    joined = " ".join(candidates.error_templates).lower()
    assert "connection pool exhausted" not in joined
    assert "deadline exceeded" not in joined


def test_a_parent_access_log_template_is_not_a_candidate(candidates):
    # "POST <*> <*>" mixes 2xx and 5xx; its 5xx refinements carry the categories
    assert not [t for t in candidates.error_templates if t.startswith(("POST ", "GET "))]


def test_candidates_are_ordered_by_volume(candidates):
    counts = [candidates.template_counts[t] for t in candidates.error_templates]
    assert counts == sorted(counts, reverse=True)
    assert min(counts) >= 5


def test_tag_keys_missing_from_the_core_properties(candidates):
    assert {"ab.cell", "platform", "nf.region"} <= set(candidates.tag_keys)
    assert {"nf.app", "app", "service"}.isdisjoint(candidates.tag_keys)
    assert candidates.tag_examples["nf.region"]


def test_prometheus_labels_are_mined_too(candidates):
    assert "state" in candidates.tag_keys


def test_an_accepted_proposal_stops_its_key_being_a_candidate(candidates):
    assert "quantile" not in candidates.tag_keys   # met:quantile, ontology/extensions/proposal-004


def test_a_tag_key_named_like_a_property_is_covered(preprocessed_hero, harvested_hero_store,
                                                    tmp_path):
    proc = tmp_path / "proc"
    shutil.copytree(preprocessed_hero.proc, proc)
    atlas = pl.read_parquet(proc / "metrics_atlas.parquet").head(1).with_columns(
        tags=pl.lit(json.dumps({"metric_name": "x", "kind": "svc"})))
    pl.concat([atlas, pl.read_parquet(proc / "metrics_atlas.parquet")]).write_parquet(
        proc / "metrics_atlas.parquet")
    found = mine_candidates(proc, harvested_hero_store)
    assert "metric_name" not in found.tag_keys    # met:metricName
    assert "kind" in found.tag_keys               # no property of that name on a series
    assert "status" in found.tag_keys             # chg:status belongs to changes, not series


def test_event_types_without_a_change_kind(preprocessed_hero, harvested_hero_store, tmp_path,
                                           candidates):
    assert candidates.event_types == []        # every hero event maps to a change kind
    proc = tmp_path / "proc"
    shutil.copytree(preprocessed_hero.proc, proc)
    events = pl.read_parquet(proc / "events.parquet")
    odd = events.filter(pl.col("event_kind") == "spinnaker").head(1).with_columns(
        event_kind=pl.lit("feature_flag"), event_type=pl.lit("FlagToggled"))
    pl.concat([events, odd]).write_parquet(proc / "events.parquet")
    assert mine_candidates(proc, harvested_hero_store).event_types == ["feature_flag:FlagToggled"]


# --- the gate -----------------------------------------------------------------

def test_proposal_redefining_a_term_is_rejected(tmp_path, harvested_hero_store, fixture_expected):
    p = write(tmp_path / "001.ttl", "ops:Application owl:equivalentClass ops:Team .")
    ok, report = check_proposal(p, harvested_hero_store, fixture_expected)
    assert not ok
    assert "ops:Application" in report or "https://kg.local/ops#Application" in report


def test_proposal_adding_concept_passes(tmp_path, harvested_hero_store, fixture_expected):
    p = write(tmp_path / "002.ttl", NEW_ERROR_CONCEPT_TTL)
    ok, report = check_proposal(p, harvested_hero_store, fixture_expected)
    assert ok, report


def test_checking_leaves_the_store_as_it_was(tmp_path, harvested_hero_store, fixture_expected):
    before = harvested_hero_store.quad_count()
    check_proposal(write(tmp_path / "002.ttl", NEW_ERROR_CONCEPT_TTL), harvested_hero_store,
                   fixture_expected)
    check_proposal(write(tmp_path / "001.ttl", "ops:Application owl:sameAs ops:Team ."),
                   harvested_hero_store, fixture_expected)
    assert harvested_hero_store.quad_count() == before


def test_a_proposal_that_changes_a_cq_answer_is_rejected(tmp_path, harvested_hero_store):
    cq = tmp_path / "cq"
    cq.mkdir()
    (cq / "99-error-categories.rq").write_text(
        "# Which error categories does the ontology define?\n"
        "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"
        "SELECT ?c WHERE { ?c skos:inScheme <https://kg.local/log/scheme/error_category> }\n")
    scheme = URIRef("https://kg.local/log/scheme/error_category")
    rows = [{"c": f"<{s}>"} for s in sorted(load_ontology().subjects(SKOS.inScheme, scheme))]
    (cq / "99-error-categories.expected.json").write_text(
        json.dumps({"question": "q", "vars": ["c"], "rows": rows}))
    ok, report = check_proposal(write(tmp_path / "003.ttl", NEW_ERROR_CONCEPT_TTL),
                                harvested_hero_store, cq)
    assert not ok
    assert "99-error-categories" in report


def test_a_proposal_violating_a_shape_is_rejected(tmp_path, harvested_hero_store,
                                                  fixture_expected):
    two_signals = NEW_ERROR_CONCEPT_TTL.replace("connection_refused", "feature_flag").replace(
        "ecat:", "ckind:").replace("log/scheme/error_category", "chg/scheme/change_kind") \
        .replace("skos:definition", "ops:signalType <https://kg.local/ops/signal_type/config_change>,"
                 " <https://kg.local/ops/signal_type/deployment> ;\n    skos:definition")
    ok, report = check_proposal(write(tmp_path / "004.ttl", two_signals), harvested_hero_store,
                                fixture_expected)
    assert not ok
    assert "shacl" in report.lower() and "signalType" in report


@pytest.mark.parametrize("ttl, why", [
    ("ops:Application rdfs:comment \"redefined\" .", "existing term"),
    ("ops:Team owl:equivalentClass ops:NewTeam .\nops:NewTeam a owl:Class ; rdfs:label \"x\" ; rdfs:comment \"y\" .",
     "existing term"),
    ("ops:Fresh a owl:Class ; rdfs:label \"f\" ; rdfs:comment \"c\" ; owl:equivalentClass ops:Team .",
     "equivalence"),
    ("ops:Fresh a owl:Class ; rdfs:label \"f\" ; rdfs:comment \"c\" ; owl:sameAs ops:Team .",
     "equivalence"),
    ("<https://kg.local/zzz#Fresh> a owl:Class ; rdfs:label \"f\" ; rdfs:comment \"c\" .",
     "namespace"),
    ("<https://kg.local/ops/application/made-up> a owl:Class ; rdfs:label \"f\" ; rdfs:comment \"c\" .",
     "namespace"),
    ("ops:Fresh a ops:Application .", "type"),
    ("ops:Fresh a owl:Class ; rdfs:comment \"c\" .", "label"),
    ("[ a owl:Class ; rdfs:label \"anon\" ] rdfs:comment \"c\" .", "blank"),
    ("<https://kg.local/chat/message_category/rumor> a skos:Concept ;"
     " skos:inScheme <https://kg.local/chat/scheme/message_category> ;"
     " skos:notation \"rumor\" ; skos:prefLabel \"Rumor\"@en ; skos:definition \"d\"@en .",
     "closed"),
    ("<https://kg.local/chat/sentiment/glad> a skos:Concept ;"
     " skos:inScheme <https://kg.local/chat/scheme/sentiment> ;"
     " skos:notation \"glad\" ; skos:prefLabel \"Glad\"@en ; skos:definition \"d\"@en .",
     "closed"),
    ("ecat:oops a skos:Concept ; skos:inScheme <https://kg.local/log/scheme/error_category> ;"
     " skos:notation \"different\" ; skos:prefLabel \"Oops\"@en ; skos:definition \"d\"@en .",
     "notation"),
    ("ckind:rollback a skos:Concept ; skos:inScheme <https://kg.local/chg/scheme/change_kind> ;"
     " skos:notation \"rollback\" ; skos:prefLabel \"Rollback\"@en ; skos:definition \"d\"@en .",
     "signal type"),
], ids=lambda v: v if isinstance(v, str) and len(v) < 18 else None)
def test_guard_rejects(tmp_path, harvested_hero_store, fixture_expected, ttl, why):
    ok, report = check_proposal(write(tmp_path / "010.ttl", ttl), harvested_hero_store,
                                fixture_expected)
    assert not ok
    assert why in report.lower()


def test_a_syntax_error_is_a_rejection_not_a_crash(tmp_path, harvested_hero_store, fixture_expected):
    ok, report = check_proposal(write(tmp_path / "011.ttl", "foo:Fresh a owl:Class ."),
                                harvested_hero_store, fixture_expected)
    assert not ok and "turtle" in report.lower()


def test_new_change_kind_with_signal_type_and_property_with_domain_pass(
        tmp_path, harvested_hero_store, fixture_expected):
    ttl = """
ckind:feature_flag a skos:Concept ; skos:inScheme <https://kg.local/chg/scheme/change_kind> ;
    skos:topConceptOf <https://kg.local/chg/scheme/change_kind> ; skos:notation "feature_flag" ;
    skos:prefLabel "Feature flag"@en ; skos:definition "A feature flag toggle."@en ;
    ops:signalType <https://kg.local/ops/signal_type/config_change> .
<https://kg.local/chg/scheme/change_kind> skos:hasTopConcept ckind:feature_flag .
met:region a owl:DatatypeProperty ; rdfs:domain ops:MetricSeries ; rdfs:range xsd:string ;
    rdfs:label "region" ; rdfs:comment "The region tag of a series." .
"""
    ok, report = check_proposal(write(tmp_path / "012.ttl", ttl), harvested_hero_store,
                                fixture_expected)
    assert ok, report


# --- proposing ----------------------------------------------------------------

def test_propose_writes_numbered_files_with_the_rationale_header(candidates, tmp_path):
    llm = DeterministicClient()
    out = tmp_path / "proposals"
    made = propose(candidates, llm, out)
    assert [p.path.name for p in made] == ["001.ttl", "002.ttl"]
    text = (out / "001.ttl").read_text()
    assert text.startswith("# ")
    assert "connection refused has no concept" in text.splitlines()[0] + text
    g = Graph().parse(out / "001.ttl", format="turtle")       # self-contained: prefixes included
    assert len(g) > 0
    assert made[0].examples and made[0].rationale


def test_propose_numbers_continue_after_existing_files(candidates, tmp_path):
    out = tmp_path / "proposals"
    out.mkdir()
    (out / "007.ttl").write_text("# earlier\n")
    made = propose(candidates, DeterministicClient(), out)
    assert made[0].path.name == "008.ttl"


def test_propose_sends_one_request_per_non_empty_kind_and_names_the_existing_terms(
        candidates, tmp_path):
    llm = DeterministicClient()
    propose(candidates, llm, tmp_path / "p")
    assert [r["kind"] for r in llm.requests] == ["error_templates", "tag_keys"]
    first = llm.requests[0]
    assert "pool_exhaustion" in json.dumps(first["existing"])
    assert any("connection refused" in c["template"].lower() for c in first["candidates"])


def test_propose_with_nothing_to_cover_makes_no_call(tmp_path):
    llm = DeterministicClient()
    assert propose(Candidates([], [], []), llm, tmp_path / "p") == []
    assert llm.requests == []


def test_a_proposed_file_passes_the_gate(candidates, tmp_path, harvested_hero_store,
                                         fixture_expected):
    made = propose(candidates, DeterministicClient(), tmp_path / "p")
    for m in made:
        ok, report = check_proposal(m.path, harvested_hero_store, fixture_expected)
        assert ok, report


# --- CLI ----------------------------------------------------------------------

@pytest.fixture
def opens_the_fixture_store(monkeypatch, harvested_hero_store):
    """The fixture store is already open in this process; RocksDB allows one opener."""
    monkeypatch.setattr("e2ekg.graph.store.Store", lambda path, **kw: harvested_hero_store)


def test_cli_check_exit_code(tmp_path, opens_the_fixture_store, fixture_expected, capsys):
    good = write(tmp_path / "002.ttl", NEW_ERROR_CONCEPT_TTL)
    bad = write(tmp_path / "001.ttl", "ops:Application owl:equivalentClass ops:Team .")
    common = ["--store", str(tmp_path), "--cq-dir", str(fixture_expected)]
    assert cli.main(["ontology", "check", str(bad), *common]) == 1
    assert "REJECT" in capsys.readouterr().out
    assert cli.main(["ontology", "check", str(good), *common]) == 0
    assert "ACCEPT" in capsys.readouterr().out


def test_cli_propose_uses_the_chosen_llm_and_reports_verdicts(
        preprocessed_hero, opens_the_fixture_store, fixture_expected, tmp_path, monkeypatch,
        capsys):
    monkeypatch.setattr(cli, "make_llm", lambda kind: DeterministicClient())
    out = tmp_path / "proposals"
    rc = cli.main(["ontology", "propose", "--proc", str(preprocessed_hero.proc), "--store",
                   str(tmp_path), "--out", str(out), "--llm", "fireworks",
                   "--cq-dir", str(fixture_expected)])
    text = capsys.readouterr().out
    assert rc == 0
    assert sorted(p.name for p in out.glob("*.ttl")) == ["001.ttl", "002.ttl"]
    assert "001.ttl" in text and "ACCEPT" in text
