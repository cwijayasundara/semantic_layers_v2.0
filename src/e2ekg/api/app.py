"""FastAPI service over the harvested stores under the data root (E2EKG_DATA, default `data`).

Read endpoints open each scenario's stores read-only and go through the agent's own graph tools
(`make_tools`), so what the UI shows is what the agent can see:

  GET  /api/status                         data root, scenarios, Fireworks key present
  GET  /api/scenarios                      per scenario: quads, SHACL, as-of cutoff, live alert
  GET  /api/scenarios/{s}                  live symptom, ranked causal chains, changes near it
  GET  /api/scenarios/{s}/topology         dependsOn edges with app kind and tier
  GET  /api/scenarios/{s}/alerts           alerts in the as-of store, newest first
  GET  /api/scenarios/{s}/case             RCA case graph for a saved run (?source=eval:<run>|job:<id>)
  GET  /api/scenarios/{s}/after            after the agent's cut-off, from the full store (?source=)
  GET  /api/scenarios/{s}/lineage          raw -> processed lineage of a node (?uri=)
  GET  /api/scenarios/{s}/scorecard        per-arm scorecard of an eval run (?run=)
  POST /api/scenarios/{s}/sparql           read-only SELECT on the as-of or full store
  GET  /api/ontology                       classes and SKOS taxonomies
  POST /api/scenarios/{s}/rca              start AutoRCA (Fireworks) in the background
  GET  /api/jobs/{id}                      an RCA job's state and report
  GET  /api/evals, /api/evals/{run}        eval runs: summary.md and scored results

`as_of` is the store the agent reads (what was observable at alert + 30 min); `full` is the
history store with everything harvested. The answer key (`raw/_answers`) is never read."""
import json
import os
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from e2ekg.graph.store import Store
from e2ekg.graph.uris import BASE, ONTOLOGY_GRAPH

DATA = Path(os.environ.get("E2EKG_DATA", "data"))
StoreKind = Literal["as_of", "full"]
CHANGE_LOOKBACK = timedelta(hours=6)

app = FastAPI(title="e2ekg API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# --- stores and tools ---------------------------------------------------------------------

def _scenarios() -> list[str]:
    g = DATA / "graph"
    return sorted(p.name for p in g.iterdir() if (p / "store").is_dir()) if g.is_dir() else []


def _graph_dir(scenario: str, kind: StoreKind) -> Path:
    if scenario not in _scenarios():
        raise HTTPException(404, f"no harvested scenario {scenario!r} under {DATA}/graph; "
                                 "run scripts/ingest.sh")
    d = DATA / "graph" / scenario / ("as_of" if kind == "as_of" else "")
    if not (d / "store").is_dir():
        raise HTTPException(404, f"no {kind} store for {scenario}; run scripts/ingest.sh")
    return d


@lru_cache(maxsize=32)
def _store(scenario: str, kind: StoreKind) -> Store:
    return Store(_graph_dir(scenario, kind) / "store", read_only=True)


def _harvest(scenario: str, kind: StoreKind) -> dict:
    p = _graph_dir(scenario, kind) / "harvest.json"
    return json.loads(p.read_text()) if p.exists() else {}


_TOOLS_LOCK = threading.Lock()


@lru_cache(maxsize=32)
def _tools_cached(scenario: str, kind: StoreKind) -> dict:
    from e2ekg.agent.tools import make_tools
    as_of = _harvest(scenario, kind).get("as_of")
    return make_tools(_store(scenario, kind), DATA / "processed" / scenario, scenario, "api",
                      as_of=datetime.fromisoformat(as_of) if as_of else None)


def _tools(scenario: str, kind: StoreKind = "as_of") -> dict:
    _graph_dir(scenario, kind)
    if not (DATA / "processed" / scenario / "catalog.parquet").exists():
        raise HTTPException(404, f"no processed tables for {scenario}; run scripts/ingest.sh")
    with _TOOLS_LOCK:
        return _tools_cached(scenario, kind)


def _call(scenario: str, name: str, kind: StoreKind = "as_of", **kwargs):
    """Run one graph tool and parse its JSON; a tool's `error: ...` becomes a 400."""
    out = _tools(scenario, kind)[name].invoke(kwargs)
    if out.startswith("error: "):
        raise HTTPException(400, out[7:])
    return json.loads(out)


def _live(scenario: str) -> dict | None:
    from e2ekg.preprocess.asof import live_cutoff
    try:
        c = live_cutoff(DATA / "processed" / scenario)
    except Exception:          # no live incident (e.g. a control scenario) or no tables
        return None
    return {"alert": c.alert_id, "symptom": c.symptom.id, "symptom_kind": c.symptom.kind,
            "alert_at": c.alert_at.isoformat(), "as_of": c.as_of.isoformat()}


def _tail(u: str | None) -> str | None:
    return u.rsplit("/", 1)[-1].rsplit("#", 1)[-1] if u else u


# --- read endpoints -----------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """The API has no home page; send browsers to the interactive docs."""
    return RedirectResponse("/docs")


@app.get("/api/status")
def status() -> dict:
    return {"data_root": str(DATA.resolve()), "scenarios": _scenarios(),
            "fireworks_key": bool(os.environ.get("FIREWORKS_API_KEY")),
            "jobs_running": sum(j["state"] == "running" for j in JOBS.values())}


@app.get("/api/scenarios")
def scenarios() -> list[dict]:
    out = []
    for s in _scenarios():
        full = _harvest(s, "full")
        try:
            aso = _harvest(s, "as_of")
        except HTTPException:
            aso = {}
        failed = sorted(k for k, v in (full.get("results") or {}).items() if not v.get("success"))
        out.append({"name": s, "quads": full.get("quads"), "shacl_ok": full.get("shacl_ok"),
                    "as_of_quads": aso.get("quads"), "as_of": aso.get("as_of"),
                    "failed_enrichers": failed, "live": _live(s)})
    return out


@app.get("/api/scenarios/{scenario}")
def scenario(scenario: str) -> dict:
    _graph_dir(scenario, "as_of")
    live = _live(scenario)
    if live is None:
        return {"name": scenario, "live": None, "symptom": None, "chains": [], "changes": []}
    sym = _call(scenario, "find_symptom", alert_id=live["alert"] or live["symptom"])
    chains = _call(scenario, "causal_chains", symptom_uri=sym["symptom_uri"])
    apps = sorted({sym["alert"]["app"], *(sym["incident"] or {}).get("affects", [])} - {None})
    closure: set[str] = set()
    for a in apps:
        closure |= {r["id"] for r in _call(scenario, "dependency_closure", app=a, direction="down")
                    if "id" in r}
    start = datetime.fromisoformat(sym["alert"]["at"].replace("Z", "+00:00")) - CHANGE_LOOKBACK
    changes = _call(scenario, "changes_in_window", apps=sorted(closure),
                    start=start.isoformat(), end=sym["as_of"]) if closure else []
    return {"name": scenario, "live": live, "symptom": sym,
            "chains": [{"confidence": c["conf"],
                        "links": [{"type": ty, "app": a, "at": at, "uri": u}
                                  for ty, a, at, u in c["links"]]}
                       for c in chains if "links" in c],
            "closure": sorted(closure), "changes": changes}


@app.get("/api/scenarios/{scenario}/topology")
def topology(scenario: str, store: StoreKind = "as_of") -> dict:
    st = _store(scenario, store)
    pfx = f"PREFIX ops: <{BASE}/ops#>\n"
    edges = st.query(pfx + "SELECT DISTINCT ?a ?b WHERE { ?a ops:dependsOn ?b }")
    nodes = st.query(pfx + """SELECT ?a ?kind ?tier WHERE { ?a a ops:Application .
        OPTIONAL { ?a ops:appKind ?kind } OPTIONAL { ?a ops:tier ?tier } }""")
    return {"nodes": [{"id": _tail(n["a"]), "kind": n["kind"], "tier": n["tier"]} for n in nodes],
            "edges": [{"source": _tail(e["a"]), "target": _tail(e["b"])} for e in edges]}


@app.get("/api/scenarios/{scenario}/alerts")
def alerts(scenario: str, limit: int = 100) -> list[dict]:
    rows = _store(scenario, "as_of").query(f"""PREFIX ops: <{BASE}/ops#>
        SELECT ?a ?t ?app WHERE {{ ?a a ops:Alert ; ops:atTime ?t .
        OPTIONAL {{ ?a ops:onApplication ?app }} }} ORDER BY DESC(?t) LIMIT {min(limit, 500)}""")
    return [{"id": _tail(r["a"]), "uri": r["a"], "at": r["t"], "app": _tail(r["app"])} for r in rows]


class SparqlIn(BaseModel):
    query: str
    store: StoreKind = "as_of"


@app.post("/api/scenarios/{scenario}/sparql")
def sparql(scenario: str, body: SparqlIn) -> dict:
    """A read-only SELECT, prefixes filled in and LIMIT capped (the agent's sparql_select)."""
    t = time.perf_counter()
    rows = _call(scenario, "sparql_select", body.store, query=body.query)
    notes = {}
    if rows and {"truncated", "capped"} & set(rows[-1]):
        last = dict(rows[-1])
        notes = {k: last.pop(k) for k in ("truncated", "capped") if k in last}
        rows = rows[:-1] + ([last] if last else [])
    return {"rows": rows, "count": len(rows), **notes,
            "ms": round((time.perf_counter() - t) * 1000, 1)}


@app.get("/api/ontology")
def ontology() -> dict:
    """Classes (with subClassOf) and SKOS concept schemes from the ontology graph."""
    s = _scenarios()
    if not s:
        raise HTTPException(404, f"no harvested store under {DATA}/graph; run scripts/ingest.sh")
    st = _store(s[0], "full")
    pfx = ("PREFIX owl: <http://www.w3.org/2002/07/owl#> "
           "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
           "PREFIX skos: <http://www.w3.org/2004/02/skos/core#> ")
    g = f"<{ONTOLOGY_GRAPH}>"
    classes = st.query(pfx + f"""SELECT ?c ?label ?comment ?parent WHERE {{ GRAPH {g} {{
        ?c a owl:Class . OPTIONAL {{ ?c rdfs:label ?label }} OPTIONAL {{ ?c rdfs:comment ?comment }}
        OPTIONAL {{ ?c rdfs:subClassOf ?parent . FILTER(isIRI(?parent)) }} }} }} ORDER BY ?c""")
    props = st.query(pfx + f"""SELECT ?p ?kind ?label ?domain ?range WHERE {{ GRAPH {g} {{
        VALUES ?kind {{ owl:ObjectProperty owl:DatatypeProperty }} ?p a ?kind .
        OPTIONAL {{ ?p rdfs:label ?label }} OPTIONAL {{ ?p rdfs:domain ?domain }}
        OPTIONAL {{ ?p rdfs:range ?range }} }} }} ORDER BY ?p""")
    concepts = st.query(pfx + f"""SELECT ?scheme ?slabel ?c ?notation ?label ?def ?broader
        WHERE {{ GRAPH {g} {{ ?scheme a skos:ConceptScheme . OPTIONAL {{ ?scheme skos:prefLabel ?slabel }}
        ?c skos:inScheme ?scheme . OPTIONAL {{ ?c skos:notation ?notation }}
        OPTIONAL {{ ?c skos:prefLabel ?label }} OPTIONAL {{ ?c skos:definition ?def }}
        OPTIONAL {{ ?c skos:broader ?broader }} }} }} ORDER BY ?scheme ?notation""")
    by_class: dict[str, dict] = {}
    for r in classes:
        c = by_class.setdefault(r["c"], {"uri": r["c"], "label": r["label"] or _tail(r["c"]),
                                         "comment": r["comment"], "parents": []})
        if r["parent"] and r["parent"] not in c["parents"]:
            c["parents"].append(r["parent"])
    schemes: dict[str, dict] = {}
    for r in concepts:
        sc = schemes.setdefault(r["scheme"], {"uri": r["scheme"],
                                              "label": r["slabel"] or _tail(r["scheme"]),
                                              "concepts": {}})
        sc["concepts"].setdefault(r["c"], {"uri": r["c"], "notation": r["notation"],
                                           "label": r["label"], "definition": r["def"],
                                           "broader": r["broader"]})
    return {"classes": list(by_class.values()),
            "properties": [{"uri": p["p"], "kind": _tail(p["kind"]), "label": p["label"],
                            "domain": p["domain"], "range": p["range"]} for p in props],
            "schemes": [{**v, "concepts": list(v["concepts"].values())} for v in schemes.values()]}


# --- AutoRCA jobs ---------------------------------------------------------------------------

JOBS: dict[str, dict] = {}
_POOL = ThreadPoolExecutor(max_workers=int(os.environ.get("E2EKG_RCA_WORKERS", "1")))


class RcaIn(BaseModel):
    alert: str | None = None       # default: the scenario's live alert


def _run_job(job_id: str) -> None:
    from e2ekg.agent.autorca import run_rca
    from e2ekg.agent.trail import Trail
    job = JOBS[job_id]
    job["state"], job["started"] = "running", time.time()
    trail = Trail()
    try:
        report = run_rca(job["alert"], job["scenario"], root=DATA, trail=trail)
        job["trail"] = trail.dump()
        job["report"] = json.loads(report.model_dump_json())
        job["state"] = "done"
    except Exception as e:     # surfaced to the UI; the server keeps running
        job["state"], job["error"] = "failed", f"{type(e).__name__}: {e}"
        job["trace"] = traceback.format_exc()[-4000:]
        job["trail"] = trail.dump()
    job["finished"] = time.time()


@app.post("/api/scenarios/{scenario}/rca")
def start_rca(scenario: str, body: RcaIn) -> dict:
    _graph_dir(scenario, "as_of")
    if not os.environ.get("FIREWORKS_API_KEY"):
        raise HTTPException(400, "AutoRCA needs FIREWORKS_API_KEY (set it in .env and restart "
                                 "the backend)")
    alert = body.alert or (_live(scenario) or {}).get("alert")
    if not alert:
        raise HTTPException(400, f"{scenario} has no live alert; pass one")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"id": job_id, "scenario": scenario, "alert": alert, "state": "queued",
                    "created": time.time()}
    _POOL.submit(_run_job, job_id)
    return JOBS[job_id]


@app.get("/api/jobs")
def jobs() -> list[dict]:
    return sorted(({k: v for k, v in j.items() if k not in ("report", "trace", "trail")}
                   for j in JOBS.values()), key=lambda j: -j["created"])


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> dict:
    if job_id not in JOBS:
        raise HTTPException(404, f"no job {job_id}")
    return JOBS[job_id]


# --- evals ----------------------------------------------------------------------------------

def _evals_dir() -> Path:
    return DATA / "evals"


@app.get("/api/evals")
def evals() -> list[dict]:
    d = _evals_dir()
    runs = sorted((p for p in d.iterdir() if p.is_dir() and (p / "summary.md").exists()),
                  key=lambda p: p.stat().st_mtime, reverse=True) if d.is_dir() else []
    out = []
    for p in runs:
        meta = json.loads((p / "meta.json").read_text()) if (p / "meta.json").exists() else {}
        out.append({"name": p.name, "has_results": (p / "results.parquet").exists(), "meta": meta})
    return out


@app.get("/api/evals/{run}")
def eval_run(run: str) -> dict:
    p = _evals_dir() / run
    if "/" in run or ".." in run or not (p / "summary.md").exists():
        raise HTTPException(404, f"no eval run {run!r}")
    rows: list[dict] = []
    if (p / "results.parquet").exists():
        import polars as pl
        rows = pl.read_parquet(p / "results.parquet").to_dicts()
    return {"name": run, "summary_md": (p / "summary.md").read_text(), "results": rows}


# --- RCA case view --------------------------------------------------------------------------

NO_RUN = ("no saved RCA run for {s}: run `uv run e2ekg eval --scenarios {s} --arms graph "
          "--runs 1` or start one from the AutoRCA tab")
SCRIPTED_CHAT_NOTE = "claims extracted by scripted stand-in"


def _eval_runs(scenario: str) -> list[Path]:
    """Eval run dirs holding a saved graph-arm run for the scenario, newest first."""
    d = _evals_dir()
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir()
                   if p.is_dir() and (p / "runs" / f"{scenario}__graph__0.json").exists()),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def _run_for(scenario: str, source: str = "") -> tuple[dict, Path | None]:
    """The saved run behind `source` and its eval run dir (None for a job)."""
    if source.startswith("job:"):
        j = JOBS.get(source[4:])
        if not j or j["scenario"] != scenario or j["state"] != "done":
            raise HTTPException(404, f"no finished AutoRCA job {source[4:]!r} for {scenario}")
        return {"report": j["report"], "alert": j["alert"], "trail": j.get("trail")}, None
    if source.startswith("eval:"):
        name = source[5:]
        if "/" in name or ".." in name or not name:
            raise HTTPException(404, f"no saved run {name!r} for {scenario}")
        p = _evals_dir() / name
        f = p / "runs" / f"{scenario}__graph__0.json"
        if not f.exists():
            raise HTTPException(404, f"no saved run {name!r} for {scenario}")
    elif source:
        raise HTTPException(404, f"unknown source {source!r}; use eval:<run> or job:<id>")
    else:
        runs = _eval_runs(scenario)
        if not runs:
            raise HTTPException(404, NO_RUN.format(s=scenario))
        p = runs[0]
        f = p / "runs" / f"{scenario}__graph__0.json"
    return json.loads(f.read_text()), p


def _chat_note(run_dir: Path | None) -> str | None:
    try:
        meta = json.loads((run_dir / "meta.json").read_text()) if run_dir else {}
        return SCRIPTED_CHAT_NOTE if "ScriptedClient" in str(meta.get("chat")) else None
    except (OSError, ValueError, AttributeError):
        return None


@app.get("/api/scenarios/{scenario}/case")
def case(scenario: str, source: str = "") -> dict:
    from e2ekg.explain.case import build_case
    run, run_dir = _run_for(scenario, source)
    tools = _tools(scenario, "as_of")
    try:
        out = build_case(_store(scenario, "as_of"), tools, scenario, run,
                         chat_note=_chat_note(run_dir))
    except ValueError as e:
        raise HTTPException(404, str(e))
    out["meta"]["source"] = f"eval:{run_dir.name}" if run_dir else source
    return out


@app.get("/api/scenarios/{scenario}/case/expand")
def case_expand(scenario: str, uri: str) -> dict:
    """One hop of the as-of graph around `uri`: the triples that name it, as case-view nodes
    and edges, so the evidence graph can be walked one claim at a time."""
    from e2ekg.explain.case import expand_node
    try:
        return expand_node(_store(scenario, "as_of"), uri)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/scenarios/{scenario}/raw")
def raw_record(scenario: str, id: str, limit: int = 20) -> dict:
    """The raw lines behind one raw id (the agent's raw_evidence tool in id mode): the
    drill-through for the evidence the report cites as raw ids. raw_evidence returns a bare
    row list (truncation noted on the last row), so it is wrapped as {rows} here."""
    try:
        rows = _call(scenario, "raw_evidence", uri_or_selector=id.strip(),
                     limit=max(1, min(int(limit), 100)))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"rows": rows, "count": len(rows)} if isinstance(rows, list) else rows


@app.get("/api/scenarios/{scenario}/after")
def after(scenario: str, source: str = "") -> dict:
    """The run's case, continued past its cut-off: the symptom and chain metrics and the
    changes after `as_of`, read from the full store and labelled after the cut-off."""
    from e2ekg.explain.after import after_cutoff
    from e2ekg.explain.case import build_case
    run, _ = _run_for(scenario, source)
    tools = _tools(scenario, "as_of")
    try:
        meta = build_case(_store(scenario, "as_of"), tools, scenario, run)["meta"]
    except ValueError as e:
        raise HTTPException(404, str(e))
    top = (run["report"].get("hypotheses") or [{}])[0]
    chain = [u.strip() for u in top.get("chain", [])]
    return after_cutoff(_store(scenario, "full"), DATA / "processed" / scenario, meta, chain)


@app.get("/api/scenarios/{scenario}/lineage")
def lineage(scenario: str, uri: str) -> dict:
    from e2ekg.explain.lineage import lineage as _lineage
    from e2ekg.preprocess.asof import live_cutoff
    _store(scenario, "as_of")
    proc = DATA / "processed" / scenario
    tools = _tools(scenario, "as_of")
    as_of = _harvest(scenario, "as_of").get("as_of")      # the tools' cutoff, as _tools_cached
    return _lineage(tools, proc, uri, as_of=datetime.fromisoformat(as_of) if as_of
                    else live_cutoff(proc).as_of)


@app.get("/api/scenarios/{scenario}/scorecard")
def scorecard(scenario: str, run: str = "") -> dict:
    from e2ekg.explain.scorecard import scorecard as _scorecard
    if run:
        if "/" in run or ".." in run:
            raise HTTPException(404, f"no scored eval run {run!r}")
        p = _evals_dir() / run
    else:
        p = _run_for(scenario, "")[1]     # the run /case shows by default
    if not (p / "results.parquet").exists():
        raise HTTPException(404, f"eval run {p.name!r} has no results.parquet; score it with "
                                 f"`uv run e2ekg eval --run-dir {p} --summarize-only`")
    return _scorecard(p, scenario)
