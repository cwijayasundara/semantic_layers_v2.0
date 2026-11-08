"""The case-view payload for one alert: `{meta, nodes, edges, trail}` assembled from the as-of
store through the agent's own tools, and the agent's saved report.

Computed facts (`facts`, from `candidate_evidence`) and the agent's prose (`agent_reason`, from
`refuted_alternatives`) are separate fields. Nothing here reads the answer key."""
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta

from e2ekg.explain.after import _changes
from e2ekg.explain.refuted import parse_refuted
from e2ekg.graph.store import Store
from e2ekg.graph.uris import BASE, NS
from e2ekg.rawid import RawSpan

APP = BASE + "/app/application/"
GRAPH = BASE + "/graph/"
WINDOW = timedelta(hours=6)
CHAT_KINDS = ("hypothesis", "mitigation", "retraction", "root_cause")
TELEMETRY = ("metric", "trace", "log")
_CAT = {"app": "app", "chg": "change", "met": "metric", "trc": "trace", "log": "log",
        "alrt": "alert", "inc": "incident", "chat": "chat"}
_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_SKIP = {_RDF_TYPE, str(NS["ops"]["rawSelector"])}
_APP_PREDS = [str(NS["ops"][p]) for p in ("onApplication", "changedApp")] + [str(NS["chat"]["about"])]
_AT, _CALLER = str(NS["ops"]["atTime"]), str(NS["trc"]["caller"])
_IRI = re.compile(r"[^\s<>\"{}|\\^`]+")
_DEPENDS = str(NS["ops"]["dependsOn"])
FINDING_SOURCE = "agent finding (run-time)"
_LABEL = [str(NS["chg"]["changeId"]), "http://www.w3.org/2000/01/rdf-schema#label",
          str(NS["chat"]["quote"])]


_NOTES = ("truncated", "capped")


def _call(tools: dict, name: str, issues: list[dict], **args):
    """A tool's parsed answer, or None on an `error:` answer; errors and the truncation notes
    `_fit` appends are recorded in `issues` ({tool, issue}) so the page can say what it lacks."""
    out = tools[name].invoke(args)
    if out.startswith("error: "):
        issues.append({"tool": name, "issue": out.removeprefix("error: ")})
        return None
    data = json.loads(out)
    rows = data if isinstance(data, list) else [data]
    for r in rows:
        if isinstance(r, dict) and r.keys() & set(_NOTES):
            left = ", ".join(f"{k} {r[k]}" for k in _NOTES if k in r)
            issues.append({"tool": name, "issue": f"answer cut to fit ({left})"})
    return data


def _rows(out) -> list[dict]:
    """A tool's rows without the truncation notes `_fit` appends."""
    return [{k: v for k, v in r.items() if k not in _NOTES}
            for r in out or [] if set(r) - set(_NOTES)]


def signed_hops(store: Store, app: str) -> dict[str, int]:
    """app -> shortest ops:dependsOn depth from `app`: positive down (what it calls), negative
    up (its callers), 0 for the app itself; down wins for an app on both sides. The agent's
    `dependency_closure`, uncapped (its answer is cut to fit a prompt)."""
    down, up = defaultdict(set), defaultdict(set)
    for r in store.query(f"SELECT DISTINCT ?a ?b WHERE {{ ?a <{_DEPENDS}> ?b }}"):
        if r["a"].startswith(APP) and r["b"].startswith(APP):
            a, b = r["a"][len(APP):], r["b"][len(APP):]
            down[a].add(b)
            up[b].add(a)

    def bfs(adj: dict[str, set[str]]) -> dict[str, int]:
        depth, frontier = {app: 0}, [app]
        while frontier:
            nxt = []
            for a in frontier:
                for b in sorted(adj[a]):
                    if b not in depth:
                        depth[b] = depth[a] + 1
                        nxt.append(b)
            frontier = nxt
        return depth

    return {a: -h for a, h in bfs(up).items()} | bfs(down)


def _when(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _tail(u: str) -> str:
    return u.rstrip("/").rsplit("/", 1)[-1]


def _app_of(u: str | None) -> str | None:
    return u[len(APP):] if u and u.startswith(APP) else u


def _cat(u: str) -> str:
    if "/ops/finding/" in u:
        return "finding"
    if not u.startswith(BASE + "/"):
        return "other"
    return _CAT.get(u[len(BASE) + 1:].split("/", 1)[0], "other")


def _curie(iri: str) -> str:
    for p, ns in NS.items():
        if iri.startswith(str(ns)):
            return f"{p}:{iri[len(str(ns)):]}"
    return iri


def _describe(store: Store, uris) -> dict[str, dict]:
    """uri -> {types, props (curie -> value or list), graph tail} for the URIs in the store."""
    uris = sorted(u for u in set(uris) if u.startswith(BASE + "/") and _IRI.fullmatch(u))
    if not uris:
        return {}
    rows = store.query("SELECT ?s ?p ?o ?g WHERE { VALUES ?s { "
                       + " ".join(f"<{u}>" for u in uris) + " } GRAPH ?g { ?s ?p ?o } }")
    out: dict[str, dict] = {}
    for r in rows:
        d = out.setdefault(r["s"], {"types": set(), "raw": {}, "graph": None})
        if r["p"] == _RDF_TYPE:
            d["types"].add(_tail(r["o"].replace("#", "/")))
        d["raw"].setdefault(r["p"], [])
        if r["o"] not in d["raw"][r["p"]]:
            d["raw"][r["p"]].append(r["o"])
        if d["graph"] is None and r["g"].startswith(GRAPH):
            d["graph"] = r["g"][len(GRAPH):]
    for d in out.values():
        d["props"] = {_curie(p): v[0] if len(v) == 1 else sorted(v)
                      for p, v in sorted(d["raw"].items()) if p not in _SKIP}
    return out


def _first(d: dict | None, preds: list[str]) -> str | None:
    for p in preds:
        if d and d["raw"].get(p):
            return d["raw"][p][0]
    return None


def _node(id: str, label: str, cat: str, **kw) -> dict:
    return {"id": id, "label": label, "cls": None, "cat": cat, "roles": [], "app": None,
            "at": None, "hop": None, "props": {}, "graph": None, "missing": False,
            "agent_reason": None, "facts": None, "discussed": None, "confidence": None, **kw}


def _facts(tools: dict, alert_id: str, uris: list[str],
           issues: list[dict]) -> tuple[dict[str, dict], str | None]:
    """uri -> computed facts, and the onset time, from `candidate_evidence`; asks again for any
    candidates the first answer had to leave out to fit its size cap (so its cut is not an
    issue; candidates it never answers are)."""
    facts: dict[str, dict] = {}
    onset, asked = None, list(uris)
    while asked:
        got: list[dict] = []
        out = _call(tools, "candidate_evidence", got, alert_id=alert_id, candidate_uris=asked)
        if out is None:
            issues += got
            break
        onset = onset or ((out["symptom"].get("onset") or {}).get("at"))
        for c in out["candidates"]:
            if "error" not in c:
                facts[c["uri"]] = {
                    "minutes_before_onset": c["minutes_before_onset"],
                    "has_path": c["call_path"] is not None,
                    "path": [f"{h['caller']}→{h['callee']}" for h in c["call_path"] or []]}
        answered = {c["uri"] for c in out["candidates"]}
        left = [u for u in asked if u not in answered]
        if len(left) == len(asked):
            issues.append({"tool": "candidate_evidence",
                           "issue": f"{len(left)} candidates left without computed facts"})
            break
        asked = left
    return facts, onset


def build_case(store: Store, tools: dict, scenario: str, run: dict, *,
               chat_note: str | None = None) -> dict:
    report = run["report"]
    hyps = [{**h, "root_cause_uri": (h.get("root_cause_uri") or "").strip() or None,
             "chain": [u.strip() for u in h.get("chain", [])],
             "evidence": [u.strip() for u in h.get("evidence", [])]}
            for h in report.get("hypotheses") or []]
    top = hyps[0] if hyps else {}
    found_root = report["verdict"] == "root_cause_found"
    issues: list[dict] = []
    found = _call(tools, "find_symptom", issues, alert_id=run["alert"])
    if found is None:
        raise ValueError(f"alert {run['alert']!r} is not in the as-of store")
    alert, incident = found["alert"], found["incident"]

    # the closure and the suspect window straight from the as-of store: the tools' answers
    # are cut to fit a prompt, and a cut closure would lose the deepest apps (and their changes)
    hops = signed_hops(store, alert["app"])
    down = sorted(a for a, h in hops.items() if h >= 0)
    start = _when(alert["at"]) - WINDOW
    window = _changes(store, down, start, _when(found["as_of"]), inclusive=True)
    what = {c["uri"]: c["what"] for c in window}

    refuted = parse_refuted([line for h in hyps for line in h.get("refuted_alternatives", [])],
                            set(what))
    reasons: dict[str, list[str]] = {}
    as_root: set[str] = set()
    for r in refuted:
        for u in r.uris:
            reasons.setdefault(u, []).append(r.reason)
            if r.as_root:
                as_root.add(u)
    root = top.get("root_cause_uri")
    chain = list(top.get("chain", []))
    evidence = list(top.get("evidence", []))
    suspects = list(dict.fromkeys([*what, *reasons]))
    named = {u for h in hyps for u in (h.get("root_cause_uri"), *h.get("chain", []),
                                       *h.get("evidence", []))} | set(reasons)
    facts, onset = _facts(tools, alert["id"], [*suspects, *([root] if root else [])], issues)

    chat = []
    if incident:
        for kind in CHAT_KINDS:
            chat += _rows(_call(tools, "chat_claims", issues, channel=incident["id"],
                                kinds=[kind]))
    trail = run.get("trail")
    findings = {f["uri"]: f for f in (trail or {}).get("findings", [])}

    graph_uris = [u for u in evidence if u.startswith(BASE + "/")]
    uris = list(dict.fromkeys([*chain, *graph_uris, *suspects, *([root] if root else []),
                               alert["uri"], *([incident["uri"]] if incident else []),
                               *(c["uri"] for c in chat),
                               *(u for f in findings.values() for u in f.get("cites", []))]))
    desc = _describe(store, uris)

    nodes: dict[str, dict] = {}
    for u in uris:
        d, cat = desc.get(u), _cat(u)
        # a finding the agent recorded during the run lives in its run-time store, not the
        # as-of store: drawn from the trail, not "missing"; it has no data time (`at` null)
        n = _node(u, what.get(u) or _first(d, _LABEL) or _tail(u), cat,
                  cls="/".join(sorted(d["types"])) or None if d else None,
                  app=_app_of(_first(d, _APP_PREDS)), at=_first(d, [_AT]),
                  props=d["props"] if d else {}, graph=d and d["graph"],
                  missing=d is None and u not in findings,
                  facts=facts.get(u), agent_reason="\n".join(reasons[u]) if u in reasons else None)
        roles = n["roles"]
        if u == root:
            roles.append("root" if found_root else "best_candidate")
            n["confidence"] = top.get("confidence")
        if u in chain:
            roles.append("chain")
            n["hop"] = chain.index(u)
        if u in graph_uris:
            roles.append("evidence")
        if u in suspects and not (found_root and u == root):
            roles.append("suspect")
        if u in as_root:
            roles.append("refuted_as_root")
        if u in (alert["uri"], (incident or {}).get("uri")):
            roles.append("symptom")
        if cat == "chat":
            roles.append("chat")
            kind = next((c["kind"] for c in chat if c["uri"] == u), None) \
                or _tail(n["props"].get("chat:claimKind") or "") or None
            n["props"] = {**n["props"], "kind": kind}
        if cat == "incident":
            n["app"] = alert["app"]       # its `affects` is the humans' first guess
        if cat == "change" or u in suspects:
            n["discussed"] = u in named or _tail(u) in report.get("summary", "")
        if u in findings:
            f = findings[u]
            n["app"], n["confidence"] = f.get("app"), f.get("confidence")
            n["props"] = {**n["props"], "claim": f.get("claim"),
                          "specialist": f.get("specialist"), "source": FINDING_SOURCE}
        nodes[u] = n
    for rid in (e for e in evidence if not e.startswith(BASE + "/")):
        try:
            path = RawSpan.parse(rid).rel_path
        except ValueError:           # neither a node URI nor a raw id: drawn, never dropped
            nodes[rid] = _node(rid, rid, "other", roles=["evidence"], missing=True)
            continue
        raw = nodes.setdefault(f"raw:{path}", _node(f"raw:{path}", path.rsplit("/", 1)[-1],
                                                    "raw", roles=["raw"]))
        if "evidence" not in raw["roles"]:
            raw["roles"].append("evidence")

    edges: dict[str, dict] = {}

    def edge(source: str, target: str, kind: str) -> None:
        edges.setdefault(f"{kind}:{source}->{target}",
                         {"id": f"{kind}:{source}->{target}", "source": source,
                          "target": target, "kind": kind})

    for a, b in zip(chain, chain[1:]):
        edge(a, b, "causal")
    if chain and incident and incident["uri"] not in chain:
        edge(chain[-1], incident["uri"], "causal")
    if incident:
        edge(alert["uri"], incident["uri"], "opened")
    for u, f in findings.items():
        for c in f.get("cites", []):
            if u in nodes and c in nodes:
                edge(u, c, "cites")

    signals = [n for n in nodes.values() if n["cat"] not in ("app", "raw")]
    for n in signals:
        if n["id"] not in desc:      # not in the as-of store: no raw lines behind it there
            continue
        for r in _rows(_call(tools, "raw_evidence", issues, uri_or_selector=n["id"], limit=3)):
            if "error" in r:
                issues.append({"tool": "raw_evidence", "issue": f"{r['raw_id']}: {r['error']}"})
            elif "raw_id" in r:
                path = RawSpan.parse(r["raw_id"]).rel_path
                nodes.setdefault(f"raw:{path}", _node(f"raw:{path}", path.rsplit("/", 1)[-1],
                                                      "raw", roles=["raw"]))
                edge(n["id"], f"raw:{path}", "prov")

    callers = {n["id"]: _app_of(_first(desc.get(n["id"]), [_CALLER])) for n in signals}
    apps = sorted({*hops, *(n["app"] for n in signals if n["app"]),
                   *(a for a in callers.values() if a), *((incident or {}).get("affects", []))})
    app_desc = _describe(store, [APP + a for a in apps])
    first_degraded: dict[str, str] = {}
    for n in signals:
        if n["cat"] in TELEMETRY and n["at"] and {"chain", "evidence"} & set(n["roles"]):
            prev = first_degraded.get(n["app"])
            if prev is None or _when(n["at"]) < _when(prev):
                first_degraded[n["app"]] = n["at"]
    for a in apps:
        d = app_desc.get(APP + a)
        nodes[APP + a] = _node(APP + a, a, "app", roles=["app"], cls="Application",
                               props=d["props"] if d else {}, graph=d and d["graph"],
                               missing=d is None, hops=hops.get(a),
                               first_degraded=first_degraded.get(a))

    for n in signals:
        if n["app"]:
            edge(n["id"], APP + n["app"], "mentions" if n["cat"] == "chat" else "onApp")
        if callers[n["id"]]:
            edge(n["id"], APP + callers[n["id"]], "caller")
    values = " ".join(f"<{APP + a}>" for a in apps)
    for r in store.query(f"""SELECT ?a ?b WHERE {{ VALUES ?a {{ {values} }}
            VALUES ?b {{ {values} }} ?a <{NS['ops']['dependsOn']}> ?b }}""") if apps else []:
        edge(r["a"], r["b"], "dependsOn")

    meta = {"scenario": scenario, "verdict": report["verdict"], "alert": alert,
            "incident": incident, "as_of": found["as_of"], "onset_at": onset,
            "summary": report.get("summary"), "confidence": top.get("confidence"),
            "arm": run.get("arm"), "seconds": run.get("seconds"),
            "model_calls": run.get("model_calls"), "tool_counts": run.get("tools", {}),
            "chat_note": chat_note, "has_trail": bool(trail),
            "unattributed_notes": [r.reason for r in refuted if not r.uris],
            "tool_issues": list({json.dumps(i, sort_keys=True): i for i in issues}.values())}
    return {"meta": meta, "nodes": list(nodes.values()), "edges": list(edges.values()),
            "trail": trail}
