"""TBox, SKOS schemes and SHACL shapes (Task 11)."""
import pyoxigraph as ox
import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from e2ekg.graph.shacl import ONTOLOGY_DIR, SCHEMES, load_ontology, validate
from e2ekg.graph.uris import NS, NS12, app_uri, change_uri, concept, scheme_uri, team_uri, uri
from e2ekg.scenario.model import SIGNAL_TYPES

OPS = NS["ops"]
CHAT = NS["chat"]

_PREFIXES = "\n".join(f"@prefix {p}: <{ns}> ." for p, ns in NS.items()) + """
@prefix ex: <https://example.org/> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
"""

# The 26 classes of DESIGN.md section 5 plus the five the brief's own
# properties and shapes need (grouping supers Change/Signal, Finding,
# CausalLink for hasLink/linkOrder, chat:Thread).
SECTION5 = ("Application Team Person Region Cluster Instance "
            "Deployment ConfigChange ScalingEvent ExperimentAllocation Commit ClientRelease "
            "MetricSeries Anomaly LogPattern LogSpike TraceEdgeFingerprint Alert "
            "Incident Page Message Claim Mitigation Hypothesis CausalChain Pattern").split()
EXTRA = {OPS.Change, OPS.Signal, OPS.Finding, OPS.CausalLink, CHAT.Thread}


def graph_from(ttl: str) -> Graph:
    return Graph().parse(data=_PREFIXES + ttl, format="turtle")


def _app(i: str = "a") -> str:
    return f"<{app_uri(i)}> a ops:Application ; ops:inCatalog true .\n"


# --- TBox --------------------------------------------------------------------

def test_ontology_parses_and_is_consistent():
    g = load_ontology()
    assert (NS["ops"].Application, RDF.type, OWL.Class) in g
    assert not list(g.subjects(RDFS.subClassOf, NS["ops"].Service))   # no generic Service type


def test_classes_are_exactly_section5_plus_documented_extras():
    g = Graph().parse(ONTOLOGY_DIR / "core.ttl")
    classes = set(g.subjects(RDF.type, OWL.Class))
    assert classes == {OPS[c] for c in SECTION5} | EXTRA


def test_no_service_class_anywhere():
    g = load_ontology()
    names = {str(c).rsplit("#", 1)[-1].rsplit("/", 1)[-1].lower()
             for c in g.subjects(RDF.type, OWL.Class)}
    assert "service" not in names
    assert (OPS.Service, None, None) not in g


def test_every_class_and_property_has_label_and_comment():
    g = Graph().parse(ONTOLOGY_DIR / "core.ttl")
    terms = set(g.subjects(RDF.type, OWL.Class))
    for kind in (OWL.ObjectProperty, OWL.DatatypeProperty):
        terms |= set(g.subjects(RDF.type, kind))
    for t in terms:
        assert g.value(t, RDFS.label) is not None, t
        assert g.value(t, RDFS.comment) is not None, t


def test_brief_key_properties_exist():
    g = load_ontology()
    for p in ("ownedBy deployedIn dependsOn affects severity changedApp atTime windowStart "
              "windowEnd onApplication rawSelector count magnitude confidence hasLink "
              "linkOrder cites resolvedBy similarTo").split():
        assert (OPS[p], RDF.type, None) in g, p
    for p in "category sentiment isIncidentRelated claimKind about retracts".split():
        assert (CHAT[p], RDF.type, None) in g, p
    assert g.value(OPS.ownedBy, RDFS.range) == OPS.Team
    assert g.value(CHAT.isIncidentRelated, RDFS.range) == NS["xsd"].boolean


# --- URIs --------------------------------------------------------------------

def test_namespaces_and_uri_scheme():
    assert NS12 == tuple("ops app team infra chg exp met log trc alrt inc chat".split())
    for p in NS12:
        assert str(NS[p]) == f"https://kg.local/{p}#"
    for p in ("prov", "skos", "owl", "time", "xsd"):
        assert p in NS
    assert str(app_uri("api-gateway")) == "https://kg.local/app/application/api-gateway"
    assert str(uri("infra", "region", "us-east-1")) == "https://kg.local/infra/region/us-east-1"
    assert str(team_uri("Team Bedrock")) == "https://kg.local/team/team/team-bedrock"
    assert (str(change_uri("hero", "config_change", "chg-cfg-0001"))
            == "https://kg.local/chg/config_change/hero/chg-cfg-0001")


@pytest.mark.parametrize("bad", [("nope", "k", "x"), ("ops", "k", ""), ("ops", "k", "has space"),
                                 ("ops", "Bad Kind", "x")])
def test_uri_rejects_bad_parts(bad):
    with pytest.raises(ValueError):
        uri(*bad)


def test_change_uri_rejects_unknown_kind():
    with pytest.raises(ValueError):
        change_uri("hero", "rollout", "chg-1")


# --- SKOS --------------------------------------------------------------------

def _members(g: Graph, scheme: str) -> set[str]:
    return {str(g.value(c, SKOS.notation)) for c in g.subjects(SKOS.inScheme, scheme_uri(scheme))}


def test_schemes_are_closed_and_verbatim():
    g = load_ontology()
    assert set(SCHEMES) == {"error_category", "fault_kind", "change_kind", "severity",
                            "message_category", "sentiment", "claim_kind", "signal_type"}
    assert _members(g, "message_category") == {"incident", "question", "discussion", "decision"}
    assert _members(g, "sentiment") == {"positive", "neutral", "negative", "urgent"}
    assert _members(g, "signal_type") == set(SIGNAL_TYPES)
    assert _members(g, "change_kind") == {"deployment", "config_change", "scaling",
                                          "experiment_allocation", "commit", "client_release"}
    assert _members(g, "severity") == {"SEV1", "SEV2", "SEV3"}
    for s in SCHEMES:
        assert (scheme_uri(s), RDF.type, SKOS.ConceptScheme) in g
        for c in g.subjects(SKOS.inScheme, scheme_uri(s)):
            assert (c, RDF.type, SKOS.Concept) in g
            assert (c, SKOS.topConceptOf, scheme_uri(s)) in g
            assert g.value(c, SKOS.prefLabel) is not None
            assert c == concept(s, str(g.value(c, SKOS.notation)))


def test_unclassified_is_a_concept_outside_the_scheme():
    g = load_ontology()
    assert (CHAT.Unclassified, RDF.type, SKOS.Concept) in g
    assert g.value(CHAT.Unclassified, SKOS.inScheme) is None


def test_change_kinds_map_to_signal_types():
    g = load_ontology()
    from e2ekg.scenario.model import CHANGE_SIGNAL
    for kind, sig in CHANGE_SIGNAL.items():
        assert g.value(concept("change_kind", kind), OPS.signalType) == concept("signal_type", sig)


# --- SHACL -------------------------------------------------------------------

def test_shapes_reject_untyped_node():
    ok, report = validate(graph_from("ex:x ops:affects ex:y ."))
    assert not ok and "rdf:type" in report


def test_shapes_reject_bad_category():
    ok, _ = validate(graph_from('ex:t a chat:Thread ; chat:category "outage" .'))
    assert not ok


def test_valid_minimal_graph_passes():
    cat = concept("message_category", "incident")
    g = graph_from(_app() + f"""
        ex:t a chat:Thread ; chat:category <{cat}> ; ops:rawSelector "slack|x|0|1|ab" .
    """)
    ok, report = validate(g)
    assert ok, report


def test_application_must_be_in_catalog():
    ok, report = validate(graph_from(f"<{app_uri('ghost')}> a ops:Application ."))
    assert not ok and "inCatalog" in report


def test_signal_subclass_needs_provenance():
    ok, _ = validate(graph_from(_app() + f"ex:s a ops:LogSpike ; ops:onApplication <{app_uri('a')}> ."))
    assert not ok
    ok, report = validate(graph_from(_app() + f"""
        ex:s a ops:LogSpike ; ops:onApplication <{app_uri('a')}> ;
             <http://www.w3.org/ns/prov#wasDerivedFrom> ex:raw .
        ex:raw a <http://www.w3.org/ns/prov#Entity> ."""))
    assert ok, report


def test_unclassified_only_when_status_unclassified():
    m = (_app() + 'ex:m a ops:Message ; ops:rawSelector "slack|x|0|1|ab" ; '
         'chat:category chat:Unclassified')
    assert not validate(graph_from(m + " .\n"))[0]
    assert not validate(graph_from(m + ' ; chat:classificationStatus "classified" .\n'))[0]
    ok, report = validate(graph_from(m + ' ; chat:classificationStatus "unclassified" .\n'))
    assert ok, report


def test_dependency_cap_both_directions():
    hub = app_uri("hub")
    many = [app_uri(f"a{i}") for i in range(51)]
    apps = "".join(_app(f"a{i}") for i in range(51)) + _app("hub")
    out = graph_from(apps + "".join(f"<{hub}> ops:dependsOn <{a}> .\n" for a in many))
    assert not validate(out)[0]
    inn = graph_from(apps + "".join(f"<{a}> ops:dependsOn <{hub}> .\n" for a in many))
    assert not validate(inn)[0]
    ok, report = validate(graph_from(apps + "".join(
        f"<{hub}> ops:dependsOn <{a}> .\n" for a in many[:50])))
    assert ok, report


@pytest.mark.parametrize("finding,good", [
    ("ops:onApplication {a} ; ops:cites ex:e ; ops:confidence 0.7", True),
    ("ops:cites ex:e ; ops:confidence 0.7", False),
    ("ops:onApplication {a} ; ops:confidence 0.7", False),
    ("ops:onApplication {a} ; ops:cites ex:e ; ops:confidence 1.2", False),
    ("ops:onApplication {a} ; ops:cites ex:e", False),
])
def test_finding_shape(finding, good):
    body = finding.format(a=f"<{app_uri('a')}>")
    g = graph_from(_app() + f"""
        ex:e a ops:LogSpike ; ops:onApplication <{app_uri('a')}> ; ops:rawSelector "r" .
        ex:f a ops:Finding ; {body} ; <http://www.w3.org/ns/prov#wasDerivedFrom> ex:e .""")
    ok, report = validate(g)
    assert ok is good, report


def test_validate_accepts_oxigraph_store():
    st = ox.Store()
    st.add(ox.Quad(ox.NamedNode("https://example.org/x"), ox.NamedNode(str(OPS.affects)),
                   ox.NamedNode("https://example.org/y"), ox.NamedNode("https://kg.local/graph/t/hero")))
    ok, report = validate(st)
    assert not ok and "rdf:type" in report


def test_severity_must_be_a_scheme_concept():
    g = graph_from(_app() + f"""
        ex:i a ops:Incident ; ops:rawSelector "r" ; ops:affects <{app_uri('a')}> ;
             ops:severity "SEV2" .""")
    assert not validate(g)[0]
    g.set((URIRef("https://example.org/i"), OPS.severity, concept("severity", "SEV2")))
    ok, report = validate(g)
    assert ok, report


def test_boolean_literal_type_for_incident_related():
    g = graph_from(_app() + 'ex:m a ops:Message ; ops:rawSelector "r" ; chat:isIncidentRelated "yes" .')
    assert not validate(g)[0]
    g.set((URIRef("https://example.org/m"), CHAT.isIncidentRelated, Literal(True)))
    assert validate(g)[0]
