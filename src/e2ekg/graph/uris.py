"""Namespaces and the one URI scheme.

Vocabulary terms (classes, properties) live in `https://kg.local/{ns}#`.
Instances live at `https://kg.local/{ns}/{kind}/{stable_id}`, where the id
comes from the catalog, a source-system id, or a content hash, never free text.

Choices:
  * Applications: `app/application/<catalog id>` (catalog ids are global).
  * Teams: `team/team/<slug of the catalog owner name>`; the name itself is the
    node's rdfs:label ("Team Bedrock" -> team-bedrock).
  * People: `team/person/<handle>`. Regions: `infra/region/<region>`.
  * Changes: `chg/<change kind>/<scenario>/<external id>`. The external id is
    the opaque id the change carries in its own system (R22): Spinnaker
    execution id, fast-property change id, ASG activity id, A/B allocation id
    or git sha. It is the only change id the raw tree (and so the graph) ever
    sees; scenario change ids such as chg-cfg-0001 stay in the answer key and
    are mapped through `_answers/<name>.json` `external_ids` (external ->
    scenario id). The kind segment keeps systems apart and is checked against
    the change kind scheme; the scenario segment keeps scenarios apart when
    stores merge.
  * Other scenario-scoped source ids (incidents, alerts, pages) follow the
    same `<scenario>/<id>` pattern via `scoped_uri`.
  * SKOS concepts: `<ns>/<scheme>/<code>`, schemes `<ns>/scheme/<scheme>`.
    The exception is chat:Unclassified, a vocabulary term deliberately
    outside every scheme.
"""
import re

from rdflib import Namespace, URIRef
from rdflib.namespace import OWL, PROV, SKOS, TIME, XSD

BASE = "https://kg.local"
NS12 = ("ops", "app", "team", "infra", "chg", "exp", "met", "log", "trc", "alrt", "inc", "chat")

NS: dict[str, Namespace] = {p: Namespace(f"{BASE}/{p}#") for p in NS12}
NS.update(prov=PROV, skos=SKOS, owl=OWL, time=TIME, xsd=XSD)

# Scheme name -> the namespace that owns its concepts.
SCHEME_NS = {
    "error_category": "log", "fault_kind": "ops", "change_kind": "chg", "severity": "inc",
    "message_category": "chat", "sentiment": "chat", "claim_kind": "chat", "signal_type": "ops",
}
CHANGE_KINDS = ("deployment", "config_change", "scaling", "experiment_allocation", "commit",
                "client_release")
ONTOLOGY_GRAPH = URIRef(f"{BASE}/graph/ontology")

_KIND = re.compile(r"[a-z][a-z0-9_]*")
_ID = re.compile(r"[A-Za-z0-9._~@:/+-]+")
_SCENARIO_CHANGE_ID = re.compile(r"chg-[a-z]+-\d+")


def uri(ns: str, kind: str, stable_id: str) -> URIRef:
    if ns not in NS12:
        raise ValueError(f"unknown namespace {ns!r}")
    if not _KIND.fullmatch(kind):
        raise ValueError(f"bad kind {kind!r}")
    if not _ID.fullmatch(stable_id) or stable_id.startswith("/") or stable_id.endswith("/"):
        raise ValueError(f"bad stable id {stable_id!r}")
    return URIRef(f"{BASE}/{ns}/{kind}/{stable_id}")


def app_uri(app_id: str) -> URIRef:
    return uri("app", "application", app_id)


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not s:
        raise ValueError(f"cannot slug {name!r}")
    return s


def team_uri(name: str) -> URIRef:
    return uri("team", "team", slug(name))


def person_uri(handle: str) -> URIRef:
    return uri("team", "person", handle)


def region_uri(region: str) -> URIRef:
    return uri("infra", "region", region)


def scoped_uri(ns: str, kind: str, scenario: str, source_id: str) -> URIRef:
    return uri(ns, kind, f"{scenario}/{source_id}")


def change_uri(scenario: str, kind: str, external_id: str) -> URIRef:
    """URI of a change from its source-system (external) id, never a scenario change id."""
    if kind not in CHANGE_KINDS:
        raise ValueError(f"unknown change kind {kind!r}")
    if _SCENARIO_CHANGE_ID.fullmatch(external_id):
        raise ValueError(f"{external_id!r} is a scenario change id; map it to its external id "
                         "through _answers/<name>.json external_ids")
    return scoped_uri("chg", kind, scenario, external_id)


def scheme_uri(scheme: str) -> URIRef:
    return uri(SCHEME_NS[scheme], "scheme", scheme)


def concept(scheme: str, code: str) -> URIRef:
    return uri(SCHEME_NS[scheme], scheme, code)
