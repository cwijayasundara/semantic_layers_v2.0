"""The flywheel: root causes a human confirmed become ops:Patterns, and the correlation
priors read them (`priors_with_history`).

A chain is confirmed when an eval row is top-1 correct AND a person approved that
conclusion, recorded in `data/evals/approvals.jsonl` as one JSON object per line:
{"scenario", "root_uri", "approved_by", "at"}. Correct is not enough: the score comes from
an answer key a real incident would not have, and the approval is what stands in for it
here. A row is matched to its approval by (scenario, root_uri), whichever arm produced it.

`adapt(store, eval_df)` writes one ops:Pattern per (change kind, application, severity) of
the confirmed chains into its own graph of `store`, `https://kg.local/graph/flywheel/evals`,
replacing that graph whole: the same inputs always give the same patterns, and withdrawing
an approval withdraws its pattern. support is the number of confirmed chains; frequency is
support over the resolved incidents of that severity in the store (those chains' own
incidents counted once) plus the confirmed chains, the same share the history enricher's
patterns state. It writes nowhere else, and never to an as-of store: the stores a live eval
reads must stay what they were observable to be at the cutoff."""
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
from rdflib import Literal, URIRef
from rdflib.namespace import PROV, RDF, RDFS

from e2ekg.graph.enricher import NS, dec, integer, node
from e2ekg.graph.store import Store, graph_uri
from e2ekg.graph.uris import concept

OPS = NS["ops"]
APPROVALS = Path("data/evals/approvals.jsonl")
FLYWHEEL_GRAPH = graph_uri("flywheel", "evals")
_PFX = ("PREFIX ops: <https://kg.local/ops#> "
        "PREFIX skos: <http://www.w3.org/2004/02/skos/core#> ")


def record_approval(scenario: str, root_uri: str, by: str, path: Path = APPROVALS) -> None:
    """Append a person's approval of `root_uri` as the root cause of `scenario`'s incident."""
    if not by.strip():
        raise ValueError("an approval names who approved it")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"scenario": scenario, "root_uri": root_uri,
                            "approved_by": by.strip(),
                            "at": datetime.now(UTC).isoformat()}) + "\n")


def load_approvals(path: Path = APPROVALS) -> set[tuple[str, str]]:
    """(scenario, root_uri) of every approval; no file means no approvals."""
    path = Path(path)
    if not path.is_file():
        return set()
    out = set()
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            a = json.loads(line)
            out.add((a["scenario"], a["root_uri"]))
            if not str(a["approved_by"]).strip():
                raise KeyError("approved_by")
        except (ValueError, KeyError, TypeError) as e:
            raise ValueError(f"{path}:{n}: not an approval ({e!r}); expected scenario, "
                             "root_uri and approved_by") from e
    return out


def confirmed_chains(eval_df: pl.DataFrame, approvals: set[tuple[str, str]]) -> pl.DataFrame:
    """Rows that are top-1 correct, have the facts a pattern needs and an approval, one per
    (scenario, root)."""
    if not eval_df.height:
        return eval_df
    ok = eval_df.filter(
        pl.col("has_root") & pl.col("top1") & pl.col("root_uri").is_not_null()
        & pl.col("root_kind").is_not_null() & pl.col("root_app").is_not_null()
        & pl.col("severity").is_not_null() & pl.col("incident_uri").is_not_null())
    return ok.filter(pl.struct("scenario", "root_uri").map_elements(
        lambda r: (r["scenario"], r["root_uri"]) in approvals, return_dtype=pl.Boolean)
    ).unique(subset=["scenario", "root_uri"], keep="first", maintain_order=True)


def _refuse_as_of(store: Store) -> None:
    harvest = store.path.parent / "harvest.json" if store.path else None
    if harvest and harvest.is_file() and json.loads(harvest.read_text()).get("as_of"):
        raise ValueError(f"{store.path} is an as-of store: the flywheel writes to the history "
                         "store, never to one a live eval reads")


def adapt(store: Store, eval_df: pl.DataFrame,
          approvals_path: Path = APPROVALS) -> list[URIRef]:
    """Write the ops:Patterns of the confirmed chains in `eval_df` into `store`; return their
    URIs. Writes nothing when no chain is confirmed (and clears what an earlier call wrote)."""
    _refuse_as_of(store)
    chains = confirmed_chains(eval_df, load_approvals(approvals_path))
    groups = defaultdict(list)
    for r in chains.iter_rows(named=True):
        groups[(r["root_kind"], r["root_app"], r["severity"])].append(r)
    mine = {r["incident_uri"] for r in chains.iter_rows(named=True)}
    total = {}
    for sev in {sev for _, _, sev in groups}:
        resolved = store.query(_PFX + f"""SELECT DISTINCT ?i WHERE {{
            ?i a ops:Incident ; ops:resolvedBy ?by ; ops:severity/skos:notation "{sev}" }}""")
        total[sev] = sum(r["i"] not in mine for r in resolved) + sum(
            len(g) for (_, _, s), g in groups.items() if s == sev)
    triples, made = [], []
    for (kind, app, sev), rows in sorted(groups.items()):
        u = node("ops", "pattern", "flywheel", kind, app, sev)
        made.append(u)
        triples += [(u, RDF.type, OPS.Pattern),
                    (u, RDFS.label, _label(kind, app, sev, len(rows))),
                    (u, OPS.causeKind, concept("change_kind", kind)),
                    (u, OPS.onApplication, URIRef(app)),
                    (u, OPS.severity, concept("severity", sev)),
                    (u, OPS.support, integer(len(rows))),
                    (u, OPS.frequency, dec(round(len(rows) / total[sev], 6)))]
        triples += [(u, PROV.wasDerivedFrom, URIRef(i)) for i in sorted({r["incident_uri"]
                                                                          for r in rows})]
    store.replace_graph(FLYWHEEL_GRAPH, triples)
    return made


def _label(kind: str, app: str, sev: str, n: int) -> Literal:
    return Literal(f"{kind}@{app.rsplit('/', 1)[-1]} {sev} (confirmed x{n})")
