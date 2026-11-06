"""Ontology evolution: mine what the ontology does not cover, have the LLM propose Turtle
extensions, and gate every proposal. Approval stays manual: a human moves an accepted
file from ontology/proposals/ to ontology/extensions/, which `load_ontology()` loads.

Mining (`mine_candidates`), three kinds:
  * error templates. WARN+ log templates the logs enricher could only file under the
    catch-all `other` category, summed across applications (access-log templates that
    have status refinements are skipped: the refinements carry the categories);
  * metric tag keys (Atlas tags, Prometheus labels) with no ontology property of that name
    usable on a metric series. The keys that name the application (`nf.app|app|service`)
    are resolved to ops:onApplication by preprocessing and are not candidates;
  * event types (event kind plus type) the changes enricher maps to no change kind.
A template that has a concept already (pool exhaustion, timeout, ...) is NOT a candidate:
the enricher's rules, not the template's words, say what is covered.

Gate (`check_proposal`), in order, each rejection explained:
  1. the file parses as Turtle (the standard prefixes are supplied);
  2. the guard: a proposal ADDS. Every subject is a new term, typed as a class, property or
     concept, in one of the 12 namespaces (concepts at their scheme URI), with a label; no
     blank nodes; no sameAs / equivalence / disjointness axioms; concepts only in the
     error_category and change_kind schemes (message category and sentiment are closed),
     with a notation equal to the URI's code, a label and a definition, and a change kind
     with a signal type. The one allowed triple on an existing term attaches a new concept
     to its scheme (skos:hasTopConcept).
  3. the CQ suite and SHACL, with the proposal loaded into the store's ontology graph
     (restored afterwards).
Why the guard when the CQ suite and SHACL already run: they query without reasoning, so an
axiom such as `ops:Application owl:equivalentClass ops:Team` changes no CQ row and violates
no shape, yet silently redefines the model for every consumer that does reason. The guard
is the only thing that sees it. Rejecting any statement about an existing term is also what
makes the gate sound: with additive-only text the CQ and SHACL runs are checking the
proposal's own terms, and a proposal can never weaken a shape or a taxonomy."""
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl
import pyoxigraph as ox
from pydantic import BaseModel, Field
from rdflib import BNode, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from e2ekg.graph.enrichers.changes import _kind as change_kind_of
from e2ekg.graph.shacl import (
    CQ_DIR, LATER_STAGES, ONTOLOGY_DIR, cq_mismatch, cq_requires, load_ontology, validate,
)
from e2ekg.graph.store import Store
from e2ekg.graph.uris import BASE, NS12, ONTOLOGY_GRAPH, SCHEME_NS, concept
from e2ekg.llm import LLMClient
from e2ekg.preprocess.metrics_clean import APP_KEYS

PROPOSALS_DIR = ONTOLOGY_DIR / "proposals"
DEFAULT_STORE = ONTOLOGY_DIR.parent / "data" / "graph" / "hero" / "store"
EXTENSIBLE_SCHEMES = ("error_category", "change_kind")
MIN_TEMPLATE_COUNT = 5
MAX_PROMPT_CANDIDATES = 30
MAX_PROPOSALS_PER_KIND = 3
EXAMPLE_VALUES = 5
OTHER = concept("error_category", "other")

PREFIXES = """\
@prefix rdf:   <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs:  <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl:   <http://www.w3.org/2002/07/owl#> .
@prefix xsd:   <http://www.w3.org/2001/XMLSchema#> .
@prefix skos:  <http://www.w3.org/2004/02/skos/core#> .
@prefix prov:  <http://www.w3.org/ns/prov#> .
""" + "".join(f"@prefix {p}: <{BASE}/{p}#> .\n" for p in NS12) + f"""\
@prefix ecat:  <{BASE}/log/error_category/> .
@prefix ckind: <{BASE}/chg/change_kind/> .
"""


# --- mining -------------------------------------------------------------------

@dataclass(frozen=True)
class Candidates:
    error_templates: list[str]
    tag_keys: list[str]
    event_types: list[str]
    template_counts: dict[str, int] = field(default_factory=dict)
    tag_examples: dict[str, list[str]] = field(default_factory=dict)


_UNCOVERED_TEMPLATES = f"""
PREFIX ops: <{BASE}/ops#>
PREFIX log: <{BASE}/log#>
SELECT ?t (SUM(?n) AS ?count) WHERE {{
  ?p a ops:LogPattern ; log:template ?t ; ops:count ?n ; ops:errorCategory <{OTHER}> .
  FILTER NOT EXISTS {{ ?refined log:refines ?p }}
}} GROUP BY ?t"""


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _series_properties(onto: Graph) -> set[str]:
    """Normalized local names of the properties a metric series can carry: declared on the
    series, its anomalies and signals in general, or on no particular class."""
    usable = {URIRef(f"{BASE}/ops#{c}") for c in ("MetricSeries", "Anomaly", "Signal")}
    props = set(onto.subjects(RDF.type, OWL.DatatypeProperty)) | set(
        onto.subjects(RDF.type, OWL.ObjectProperty))
    return {_norm(str(p).rsplit("#", 1)[-1]) for p in props
            if not (domains := set(onto.objects(p, RDFS.domain))) or domains & usable}


def _tag_values(proc: Path) -> dict[str, set[str]]:
    seen: dict[str, set[str]] = {}
    for table, col in (("metrics_atlas", "tags"), ("metrics_prom", "labels")):
        path = proc / f"{table}.parquet"
        if not path.exists():
            continue
        for raw in pl.scan_parquet(path).select(col).unique().collect()[col]:
            for k, v in (json.loads(raw) if raw else {}).items():
                vals = seen.setdefault(k, set())
                if len(vals) < 200:
                    vals.add(str(v))
    return seen


def _unmapped_events(proc: Path) -> list[str]:
    path = proc / "events.parquet"
    if not path.exists():
        return []
    ev = (pl.scan_parquet(path).filter(pl.col("row_kind") == "record")
          .select("event_kind", "event_type").unique().collect().sort("event_kind", "event_type"))
    return [f"{k}:{t}" if t else k for k, t in ev.iter_rows() if change_kind_of(k, t) is None]


def mine_candidates(proc_root: Path, store: Store, min_count: int = MIN_TEMPLATE_COUNT
                    ) -> Candidates:
    proc = Path(proc_root)
    rows = [(r["t"], int(r["count"])) for r in store.query(_UNCOVERED_TEMPLATES)]
    counts = {t: n for t, n in sorted(rows, key=lambda r: (-r[1], r[0])) if n >= min_count}
    covered = _series_properties(load_ontology())
    values = _tag_values(proc)
    keys = sorted(k for k in values if k not in APP_KEYS
                  and not {_norm(k), _norm(k.rsplit(".", 1)[-1])} & covered)
    return Candidates(
        error_templates=list(counts), tag_keys=keys, event_types=_unmapped_events(proc),
        template_counts=counts,
        tag_examples={k: sorted(values[k])[:EXAMPLE_VALUES] for k in keys})


# --- proposing ----------------------------------------------------------------

class ProposalDraft(BaseModel):
    turtle: str = Field(description="Turtle statements for the new terms only, no prose")
    rationale: str = Field(description="Why these terms are needed, in one or two sentences")
    examples: list[str] = Field(default_factory=list,
                                description="The candidates (verbatim) the terms cover")


class ProposalBatch(BaseModel):
    proposals: list[ProposalDraft]


@dataclass(frozen=True)
class OntologyProposal:
    turtle: str
    rationale: str
    examples: list[str]
    path: Path | None = None


SYSTEM = f"""You extend an observability ontology with new terms for things it cannot yet \
express. You are given candidates mined from real data and the terms that already exist. \
Answer with Turtle for NEW terms only.

Rules (a proposal that breaks one is rejected automatically):
- Add only. Never state anything about a term that already exists, and never use owl:sameAs, \
owl:equivalentClass, owl:equivalentProperty or owl:disjointWith.
- Vocabulary terms (owl:Class, owl:ObjectProperty, owl:DatatypeProperty) live in one of the \
namespaces ops app team infra chg exp met log trc alrt inc chat, IRI {BASE}/<ns>#<Name>. \
Give each an rdfs:label and an rdfs:comment. Classes are UpperCamelCase, properties lowerCamelCase.
- New error categories are skos:Concept at {BASE}/log/error_category/<code> (prefix ecat:), \
new change kinds at {BASE}/chg/change_kind/<code> (prefix ckind:), <code> in snake_case. Each \
concept needs skos:inScheme (the existing scheme IRI), skos:topConceptOf, skos:notation "<code>", \
skos:prefLabel "..."@en and skos:definition "..."@en, plus the triple \
`<scheme IRI> skos:hasTopConcept <concept> .`. A change kind also needs ops:signalType, one of \
the existing signal types.
- Other taxonomies (message category, sentiment, severity, claim kind) are closed. No blank nodes.
- Prefixes rdf rdfs owl xsd skos prov, the 12 namespaces, ecat and ckind are predeclared; do not \
write @prefix lines.
- Prefer a few general terms to one term per candidate; say in the rationale which candidates \
each covers, and list those candidates verbatim in examples. Propose nothing for a candidate the \
existing terms already express.

Example of a valid proposal:
ecat:connection_refused a skos:Concept ;
    skos:inScheme <{BASE}/log/scheme/error_category> ;
    skos:topConceptOf <{BASE}/log/scheme/error_category> ;
    skos:notation "connection_refused" ;
    skos:prefLabel "Connection refused"@en ;
    skos:definition "A peer actively refused a connection."@en .
<{BASE}/log/scheme/error_category> skos:hasTopConcept ecat:connection_refused ."""

_TASK = {
    "error_templates": "Log templates (with row counts) that only fit the catch-all `other` "
                       "error category. Propose error categories (skos:Concept) that cover "
                       "recurring families of them.",
    "tag_keys": "Metric tag keys (with example values) that no property of the ontology "
                "expresses. Propose owl:DatatypeProperty terms in the met: namespace, with "
                "rdfs:domain ops:MetricSeries and an rdfs:range.",
    "event_types": "Event kinds and types that map to no change kind. Propose change kinds "
                   "(skos:Concept) with their ops:signalType.",
}


def _existing(kind: str, onto: Graph) -> dict:
    def scheme(name: str) -> list[dict]:
        sch = URIRef(f"{BASE}/{SCHEME_NS[name]}/scheme/{name}")
        return [{"code": str(onto.value(c, SKOS.notation)),
                 "definition": str(onto.value(c, SKOS.definition) or "")}
                for c in sorted(onto.subjects(SKOS.inScheme, sch))]
    if kind == "error_templates":
        return {"error_category": scheme("error_category")}
    if kind == "event_types":
        return {"change_kind": scheme("change_kind"),
                "signal_type": [c["code"] for c in scheme("signal_type")]}
    props = set(onto.subjects(RDF.type, OWL.DatatypeProperty)) | set(
        onto.subjects(RDF.type, OWL.ObjectProperty))
    return {"properties": sorted(onto.namespace_manager.normalizeUri(p) for p in props)}


def _requests(c: Candidates) -> Iterator[tuple[str, list[dict]]]:
    top = c.error_templates[:MAX_PROMPT_CANDIDATES]
    yield "error_templates", [{"template": t, "count": c.template_counts.get(t)} for t in top]
    yield "tag_keys", [{"key": k, "examples": c.tag_examples.get(k, [])}
                       for k in c.tag_keys[:MAX_PROMPT_CANDIDATES]]
    yield "event_types", [{"event": e} for e in c.event_types[:MAX_PROMPT_CANDIDATES]]


def _next_number(out_dir: Path) -> int:
    nums = [int(p.stem) for p in out_dir.glob("*.ttl") if p.stem.isdigit()]
    return max(nums, default=0) + 1


def _render(d: ProposalDraft, kind: str) -> str:
    head = [f"# Proposal for {kind}", *(f"# {line}" for line in d.rationale.splitlines()),
            *(f"# example: {e}" for e in d.examples)]
    return "\n".join(head) + "\n\n" + PREFIXES + "\n" + d.turtle.strip() + "\n"


def propose(candidates: Candidates, llm: LLMClient, out_dir: Path = PROPOSALS_DIR
            ) -> list[OntologyProposal]:
    """One LLM call per kind that has candidates; each proposal is written to
    `<out_dir>/NNN.ttl` (rationale and examples as a comment header). A proposal is not
    vetted here: `check_proposal` is the gate."""
    out_dir = Path(out_dir)
    onto = load_ontology()
    made: list[OntologyProposal] = []
    for kind, items in _requests(candidates):
        if not items:
            continue
        user = json.dumps({"kind": kind, "task": _TASK[kind], "candidates": items,
                           "existing": _existing(kind, onto)}, sort_keys=True)
        batch = llm.structured(SYSTEM, user, ProposalBatch)
        for d in batch.proposals[:MAX_PROPOSALS_PER_KIND]:
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{_next_number(out_dir):03d}.ttl"
            path.write_text(_render(d, kind), encoding="utf-8")
            made.append(OntologyProposal(d.turtle, d.rationale, list(d.examples), path))
    return made


# --- the gate -----------------------------------------------------------------

_CLASS_TYPES = {OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty}
_NO_IDENTITY_AXIOMS = {OWL.sameAs, OWL.equivalentClass, OWL.equivalentProperty, OWL.disjointWith}
_VOCAB_NS = tuple(f"{BASE}/{p}#" for p in NS12)
_LOCAL = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_CODE = re.compile(r"[a-z][a-z0-9_]*")


def _curie(g: Graph, term) -> str:
    return g.namespace_manager.normalizeUri(term)


def _concept_scheme(s: URIRef) -> tuple[str, str] | None:
    """(scheme, code) when `s` is `<ns>/<scheme>/<code>` of a known scheme."""
    for scheme, ns in SCHEME_NS.items():
        prefix = f"{BASE}/{ns}/{scheme}/"
        if str(s).startswith(prefix):
            return scheme, str(s)[len(prefix):]
    return None


def _guard(g: Graph) -> list[str]:
    onto = load_ontology()
    known = set(onto.subjects())
    bad: list[str] = []
    new_concepts = set(g.subjects(RDF.type, SKOS.Concept))
    for s, p, o in g:
        if any(isinstance(x, BNode) for x in (s, o)):
            bad.append("blank nodes are not allowed: state each term as a named IRI")
        if p in _NO_IDENTITY_AXIOMS:
            bad.append(f"identity/equivalence axiom {_curie(g, p)} on {_curie(g, s)}: a "
                       "proposal may not merge or separate terms")
    for s in sorted(set(g.subjects()), key=str):
        if isinstance(s, BNode):
            continue
        name = _curie(g, s)
        if s in known:
            is_scheme_link = (_scheme_of_iri(s) in EXTENSIBLE_SCHEMES and all(
                p == SKOS.hasTopConcept and o in new_concepts for p, o in g.predicate_objects(s)))
            if not is_scheme_link:
                bad.append(f"{name} is an existing term; a proposal may only add new terms")
            continue
        bad.extend(_new_term_problems(g, s, name))
    return list(dict.fromkeys(bad))


def _scheme_of_iri(s: URIRef) -> str | None:
    for scheme, ns in SCHEME_NS.items():
        if str(s) == f"{BASE}/{ns}/scheme/{scheme}":
            return scheme
    return None


def _new_term_problems(g: Graph, s: URIRef, name: str) -> list[str]:
    types = set(g.objects(s, RDF.type))
    bad: list[str] = []
    if SKOS.Concept in types:
        bad += _concept_problems(g, s, name)
    elif types and types <= _CLASS_TYPES:
        if not any(str(s).startswith(ns) and _LOCAL.fullmatch(str(s)[len(ns):])
                   for ns in _VOCAB_NS):
            bad.append(f"{name}: IRI outside the 12 namespaces ({BASE}/<ns>#<Name>)")
        for pred, what in ((RDFS.label, "rdfs:label"), (RDFS.comment, "rdfs:comment")):
            if not g.value(s, pred):
                bad.append(f"{name}: missing {what} (every new term needs a label and a comment)")
    else:
        shown = ", ".join(sorted(_curie(g, t) for t in types)) or "none"
        bad.append(f"{name}: type {shown} is not a class, property or concept "
                   "(proposals carry vocabulary, not instance data)")
    return bad


def _concept_problems(g: Graph, s: URIRef, name: str) -> list[str]:
    schemes = {sch for o in g.objects(s, SKOS.inScheme) if (sch := _scheme_of_iri(o))}
    if not schemes or not schemes <= set(EXTENSIBLE_SCHEMES):
        return [f"{name}: scheme not extensible; only {', '.join(EXTENSIBLE_SCHEMES)} grow "
                "(message category, sentiment and the other taxonomies are closed)"]
    (scheme,) = schemes
    bad: list[str] = []
    found = _concept_scheme(s)
    code = str(g.value(s, SKOS.notation) or "")
    if found != (scheme, code) or not _CODE.fullmatch(code):
        bad.append(f"{name}: IRI must be {BASE}/{SCHEME_NS[scheme]}/{scheme}/<code> and "
                   "skos:notation must equal <code> (snake_case); notation mismatch")
    for pred, what in ((SKOS.prefLabel, "skos:prefLabel"), (SKOS.definition, "skos:definition")):
        if not g.value(s, pred):
            bad.append(f"{name}: missing {what}")
    if scheme == "change_kind":
        sig = list(g.objects(s, URIRef(f"{BASE}/ops#signalType")))
        known = set(load_ontology().subjects(SKOS.inScheme,
                                             URIRef(f"{BASE}/ops/scheme/signal_type")))
        if not sig or not set(sig) <= known:
            bad.append(f"{name}: a change kind needs ops:signalType, one of the existing "
                       "signal types")
    return bad


def _parse(path: Path) -> tuple[Graph | None, str]:
    g = Graph()
    try:
        g.parse(data=PREFIXES + Path(path).read_text(encoding="utf-8"), format="turtle")
    except Exception as e:
        return None, f"not valid Turtle: {e}"
    return g, ""


@contextmanager
def _ontology_with(store: Store, extra: Graph) -> Iterator[None]:
    """The store's ontology graph becomes core + schemes + extensions + `extra` for the
    duration, then is put back exactly as it was."""
    raw, name = store.oxigraph, ox.NamedNode(str(ONTOLOGY_GRAPH))
    old = list(raw.quads_for_pattern(None, None, None, name))
    raw.remove_graph(name)
    try:
        raw.load((load_ontology() + extra).serialize(format="nt"),
                 format=ox.RdfFormat.N_TRIPLES, to_graph=name)
        yield
    finally:
        raw.remove_graph(name)
        if old:
            raw.extend(old)


def _cq_problems(store: Store, cq_dir: Path) -> tuple[list[str], int]:
    bad, ran = [], 0
    for cq in sorted(Path(cq_dir).glob("*.rq")):
        if cq_requires(cq) & LATER_STAGES:
            continue
        ran += 1
        try:
            mismatch = cq_mismatch(store, cq)
        except Exception as e:
            mismatch = f"{type(e).__name__}: {e}"
        if mismatch:
            bad.append(f"CQ {cq.stem} no longer holds: {mismatch[:400]}")
    return bad, ran


def check_proposal(path: Path, store: Store | None = None, cq_dir: Path | None = None
                   ) -> tuple[bool, str]:
    """(accepted, report). `store` defaults to the full-scale hero harvest and `cq_dir` to
    ontology/cq (whose expected rows are the full-scale ones)."""
    g, err = _parse(Path(path))
    if g is None:
        return False, err
    if problems := _guard(g):
        return False, "\n".join(f"- {p}" for p in problems)
    if store is None:
        if not DEFAULT_STORE.is_dir():
            return False, (f"- the proposal passes the guard, but there is no harvested store "
                           f"at {DEFAULT_STORE} to run SHACL and the CQ suite against")
        store = Store(DEFAULT_STORE)
    with _ontology_with(store, g):
        bad, ran = _cq_problems(store, cq_dir or CQ_DIR)
        conforms, text = validate(store)
    if not conforms:
        bad.append("SHACL violations with the proposal loaded:\n" + text[:1500])
    if bad:
        return False, "\n".join(f"- {p}" for p in bad)
    return True, f"guard clean; SHACL conforms; {ran} competency questions still hold"
