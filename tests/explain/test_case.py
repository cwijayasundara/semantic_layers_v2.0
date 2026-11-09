import builtins
import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from explain_helpers import with_report

from e2ekg.explain.case import build_case

MISSING = "https://kg.local/ops/finding/hero/deadbeef"


def _call(tools, name, **args):
    return json.loads(tools[name].invoke(args))


def _build(live, run):
    return build_case(live.store, live.tools, "hero", run)


def _nodes(case):
    return {n["id"]: n for n in case["nodes"]}


def _when(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _app(case, name):
    [n] = [n for n in case["nodes"] if n["cat"] == "app" and n["label"] == name]
    return n


def _window_changes(live, case):
    alert = case["meta"]["alert"]
    closure = _call(live.tools, "dependency_closure", app=alert["app"], direction="down")
    start = datetime.fromisoformat(alert["at"].replace("Z", "+00:00")) - timedelta(hours=6)
    return [c["uri"] for c in _call(live.tools, "changes_in_window", apps=[r["id"] for r in closure],
                                    start=start.isoformat(), end=case["meta"]["as_of"])]


@pytest.fixture(scope="module")
def case(live_tools, engine_run):
    return _build(live_tools, engine_run)


def _hyp(run):
    return run["report"]["hypotheses"][0]


def test_chain_and_root_roles(case, engine_run, live_tools):
    chain = _hyp(engine_run)["chain"]
    nodes = _nodes(case)
    root = nodes[_hyp(engine_run)["root_cause_uri"]]
    assert {"root", "chain"} <= set(root["roles"])
    assert all(u in nodes for u in chain)
    incident = case["meta"]["incident"]["uri"]
    causal = [e for e in case["edges"] if e["kind"] == "causal"]
    assert len(causal) == len(chain) - 1 + (incident not in chain)

    trimmed = _build(live_tools, with_report(engine_run, chain=chain[:-1]))
    causal = [e for e in trimmed["edges"] if e["kind"] == "causal"]
    assert len(causal) == len(chain[:-1]) - 1 + 1
    assert any(e["target"] == incident for e in causal)


def test_every_window_change_is_a_suspect_with_facts(case, engine_run, live_tools):
    nodes = _nodes(case)
    root = _hyp(engine_run)["root_cause_uri"]
    window = [u for u in _window_changes(live_tools, case) if u != root]
    assert window
    for u in window:
        assert "suspect" in nodes[u]["roles"]
        assert isinstance(nodes[u]["facts"]["has_path"], bool)
    assert nodes[root]["facts"]["minutes_before_onset"] > 0
    assert nodes[root]["facts"]["path"]
    assert all("→" in hop for hop in nodes[root]["facts"]["path"])
    assert nodes[window[0]]["agent_reason"] == "after onset"
    assert case["meta"]["onset_at"]


def test_undiscussed_suspect(engine_run, live_tools):
    hyp = _hyp(engine_run)
    dropped = next(line.split()[0] for line in hyp["refuted_alternatives"]
                   if line.split()[0] not in hyp["chain"])
    kept = [line for line in hyp["refuted_alternatives"] if not line.startswith(dropped)]
    node = _nodes(_build(live_tools, with_report(engine_run, refuted_alternatives=kept)))[dropped]
    assert "suspect" in node["roles"]
    assert node["discussed"] is False
    assert node["agent_reason"] is None


def test_roles_are_a_set(engine_run, live_tools):
    hyp = _hyp(engine_run)
    hop = next(u for u in hyp["chain"][1:] if "/chg/" in u)
    refuted = hyp["refuted_alternatives"] + [f"{hop} refuted as root: a vehicle, not the cause"]
    node = _nodes(_build(live_tools, with_report(engine_run, refuted_alternatives=refuted)))[hop]
    assert {"chain", "refuted_as_root"} <= set(node["roles"])
    assert "a vehicle, not the cause" in node["agent_reason"]
    assert node["discussed"] is True


def test_first_degraded_ignores_changes_and_incident(case, engine_run):
    nodes = _nodes(case)
    root = nodes[_hyp(engine_run)["root_cause_uri"]]
    root_app = _app(case, root["app"])
    assert _when(root_app["first_degraded"]) > _when(root["at"])
    assert case["meta"]["incident"]["affects"] == ["api-gateway"]
    assert _app(case, "api-gateway")["first_degraded"] is None
    incident = nodes[case["meta"]["incident"]["uri"]]
    assert incident["app"] == case["meta"]["alert"]["app"]


def test_app_hops_are_signed(case):
    alert_app = _app(case, case["meta"]["alert"]["app"])
    assert alert_app["hops"] == 0
    hops = [n["hops"] for n in case["nodes"] if n["cat"] == "app"]
    assert any(h is not None and h < 0 for h in hops)
    assert any(h is not None and h > 0 for h in hops)
    deps = [e for e in case["edges"] if e["kind"] == "dependsOn"]
    assert deps


def test_raw_file_nodes(case, engine_run):
    root = _hyp(engine_run)["root_cause_uri"]
    assert {"id": f"prov:{root}->raw:events/properties.log", "source": root,
            "target": "raw:events/properties.log", "kind": "prov"} in case["edges"]
    raw = _nodes(case)["raw:events/properties.log"]
    assert (raw["cat"], raw["roles"], raw["label"]) == ("raw", ["raw"], "properties.log")
    assert not any(e["kind"] == "prov" and e["source"].startswith("https://kg.local/app/")
                   for e in case["edges"])


def test_missing_uri_is_drawn_not_fatal(engine_run, live_tools):
    bad = ["https://kg.local/chg/deployment/hero/bad uri>", "not a uri"]
    evidence = _hyp(engine_run)["evidence"] + [MISSING, *bad]
    nodes = _nodes(_build(live_tools, with_report(engine_run, evidence=evidence)))
    node = nodes[MISSING]
    assert node["missing"] is True
    assert node["cat"] == "finding"
    assert "evidence" in node["roles"]
    assert all(nodes[u]["missing"] is True and "evidence" in nodes[u]["roles"] for u in bad)


def test_chat_uris_from_the_report_are_drawn(engine_run, live_tools):
    from e2ekg.graph.uris import NS
    [row] = live_tools.store.query(f"""SELECT ?c WHERE {{ ?c <{NS['chat']['claimKind']}>
        <https://kg.local/chat/claim_kind/observation> }} ORDER BY ?c LIMIT 1""")
    observation, missing = row["c"], "https://kg.local/chat/claim/hero/deadbeef"
    evidence = _hyp(engine_run)["evidence"] + [observation, missing]
    nodes = _nodes(_build(live_tools, with_report(engine_run, evidence=evidence)))
    assert nodes[missing]["missing"] is True and nodes[missing]["cat"] == "chat"
    assert nodes[observation]["missing"] is False
    assert nodes[observation]["props"]["kind"] == "observation"
    assert {"chat", "evidence"} <= set(nodes[observation]["roles"])


def test_report_uris_with_whitespace_are_normalised(engine_run, live_tools):
    root = _hyp(engine_run)["root_cause_uri"]
    run = with_report(engine_run, root_cause_uri=f"  {root}\n",
                      chain=[f" {u} " for u in _hyp(engine_run)["chain"]])
    node = _nodes(_build(live_tools, run))[root]
    assert {"root", "chain"} <= set(node["roles"])
    assert node["facts"]["has_path"] is True


def test_findings_enrich_from_trail(engine_run, live_tools):
    cited = _hyp(engine_run)["root_cause_uri"]
    run = with_report(engine_run, evidence=_hyp(engine_run)["evidence"] + [MISSING])
    run["trail"] = {"steps": [], "findings": [
        {"uri": MISSING, "specialist": "changes", "app": "title-metadata-svc",
         "claim": "floor removed", "confidence": 0.7, "cites": [cited]}]}
    case = _build(live_tools, run)
    node = _nodes(case)[MISSING]
    # recorded by the agent during the run: drawn from the trail, not "missing"
    assert (node["missing"], node["app"], node["confidence"]) == (False, "title-metadata-svc", 0.7)
    assert node["props"]["claim"] == "floor removed"
    assert node["props"]["specialist"] == "changes"
    assert node["props"]["source"] == "agent finding (run-time)"
    assert node["at"] is None
    assert {"id": f"cites:{MISSING}->{cited}", "source": MISSING, "target": cited,
            "kind": "cites"} in case["edges"]
    assert case["trail"] == run["trail"] and case["meta"]["has_trail"] is True


def test_chat_claims_and_meta(case, engine_run):
    chat = [n for n in case["nodes"] if n["cat"] == "chat"]
    assert chat and all(n["roles"] == ["chat"] for n in chat)
    assert {n["props"]["kind"] for n in chat} <= {"hypothesis", "mitigation", "retraction",
                                                   "root_cause"}
    assert any(n["app"] == "api-gateway" for n in chat)
    meta = case["meta"]
    assert (meta["scenario"], meta["verdict"], meta["arm"]) == ("hero", "root_cause_found", "graph")
    assert meta["confidence"] == 0.64 and meta["tool_counts"] == engine_run["tools"]
    assert meta["has_trail"] is False and case["trail"] is None
    kinds = {e["kind"] for e in case["edges"]}
    assert kinds <= {"dependsOn", "onApp", "caller", "causal", "opened", "mentions", "cites",
                     "prov"}
    ids = {n["id"] for n in case["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in case["edges"])


def test_unattributed_note_is_kept(engine_run, live_tools):
    refuted = _hyp(engine_run)["refuted_alternatives"] + ["the gateway rollback was noise"]
    case = _build(live_tools, with_report(engine_run, refuted_alternatives=refuted))
    assert case["meta"]["unattributed_notes"] == ["the gateway rollback was noise"]


def test_inconclusive_has_no_root(engine_run, live_tools):
    run = with_report(engine_run, confidence=0.1)
    run["report"]["verdict"] = "inconclusive"
    case = _build(live_tools, run)
    assert not any("root" in n["roles"] for n in case["nodes"])
    best = _nodes(case)[_hyp(run)["root_cause_uri"]]
    assert "best_candidate" in best["roles"]
    assert best["confidence"] == 0.1
    assert case["meta"]["verdict"] == "inconclusive"


def test_never_reads_answers(engine_run, live_tools, monkeypatch):
    def guarded(fn):
        def run(self_or_path, *a, **k):
            if "_answers" in str(self_or_path):
                raise AssertionError(f"read the answer key: {self_or_path}")
            return fn(self_or_path, *a, **k)
        return run

    monkeypatch.setattr(Path, "read_text", guarded(Path.read_text))
    monkeypatch.setattr(Path, "open", guarded(Path.open))
    monkeypatch.setattr(builtins, "open", guarded(builtins.open))
    assert _build(live_tools, copy.deepcopy(engine_run))["nodes"]


def test_signed_hops_are_uncapped():
    """Bigger than the tool's caps (LIMIT rows per query, CAP characters per answer): every
    app keeps its shortest signed depth."""
    from rdflib import URIRef

    from e2ekg.agent.tools import LIMIT
    from e2ekg.explain.case import APP, signed_hops
    from e2ekg.graph.store import Store
    from e2ekg.graph.uris import NS
    dep = URIRef(str(NS["ops"]["dependsOn"]))
    n = LIMIT + 50
    pairs = [("a0", f"m{i:03}") for i in range(n)] + [(f"m{i:03}", f"d{i:03}") for i in range(n)]
    pairs += [("c", "a0"), ("cc", "c"), ("a0", "x"), ("x", "a0"), ("d000", "a0")]
    store = Store(None)
    store.add_to_graph(URIRef("https://kg.local/graph/test"),
                       [(URIRef(APP + a), dep, URIRef(APP + b)) for a, b in pairs])
    hops = signed_hops(store, "a0")
    assert hops["a0"] == 0
    assert sum(h == 1 for h in hops.values()) == n + 1          # every m, and x
    assert sum(h == 2 for h in hops.values()) == n
    assert (hops["c"], hops["cc"], hops["x"], hops["d000"]) == (-1, -2, 1, 2)


def test_case_reads_the_closure_from_the_store_not_the_capped_tool(case, engine_run, live_tools):
    """A cut `dependency_closure`/`changes_in_window` answer must not cost apps their hops
    (the root would land in the no-path band) or the suspects their place."""
    capped = dict(live_tools.tools)
    for name in ("dependency_closure", "changes_in_window"):
        capped[name] = SimpleNamespace(invoke=lambda args: '[{"truncated": 99}]')
    other = build_case(live_tools.store, capped, "hero", engine_run)
    assert {n["id"]: n.get("hops") for n in other["nodes"] if n["cat"] == "app"} \
        == {n["id"]: n.get("hops") for n in case["nodes"] if n["cat"] == "app"}
    assert {n["id"] for n in other["nodes"] if "suspect" in n["roles"]} \
        == {n["id"] for n in case["nodes"] if "suspect" in n["roles"]}


def test_apps_on_a_path_have_hops(case):
    nodes = _nodes(case)
    on_path = {nodes[u]["app"] for u in _hyp_chain(case) if nodes[u]["app"]}
    for n in case["nodes"]:
        for hop in (n["facts"] or {}).get("path") or []:
            on_path |= set(hop.split("→"))
    assert on_path
    for a in on_path:
        assert _app(case, a)["hops"] is not None, a


def _hyp_chain(case):
    return [n["id"] for n in sorted((n for n in case["nodes"] if n["hop"] is not None),
                                    key=lambda n: n["hop"])]


def test_tool_errors_and_cuts_are_reported(engine_run, live_tools):
    tools = dict(live_tools.tools)
    tools["chat_claims"] = SimpleNamespace(invoke=lambda args: "error: chat store offline")
    case = build_case(live_tools.store, tools, "hero", engine_run)
    assert {"tool": "chat_claims", "issue": "chat store offline"} in case["meta"]["tool_issues"]
    assert not [n for n in case["nodes"] if n["cat"] == "chat"]


def test_no_tool_issues_on_a_clean_case(case):
    assert case["meta"]["tool_issues"] == []


def test_hypotheses_carry_per_hypothesis_argument_graphs(engine_run, live_tools):
    import copy
    run = copy.deepcopy(engine_run)
    h0 = run["report"]["hypotheses"][0]
    other = h0["refuted_alternatives"][0].split(" refuted:", 1)[0].strip()
    run["report"]["hypotheses"].append(
        {"root_cause_uri": other, "chain": [other], "confidence": 0.4, "evidence": [other],
         "refuted_alternatives": [f"{h0['root_cause_uri']} refuted: "
                                 "landed after the degradation began"]})
    case = _build(live_tools, run)
    hyps = case["hypotheses"]
    assert [h["i"] for h in hyps] == [0, 1]
    assert hyps[0]["root"] == h0["root_cause_uri"] and hyps[1]["root"] == other
    ids0 = {n["id"] for n in hyps[0]["nodes"]}
    assert set(h0["chain"]) <= ids0
    assert any(e["kind"] == "causal" for e in hyps[0]["edges"])
    assert any(e["kind"] == "refutes" and e["source"] == other for e in hyps[0]["edges"])
    h1 = next(n for n in hyps[1]["nodes"] if n["id"] == other)
    assert "h1:root" in h1["roles"] and h1["confidence"] == 0.4
    assert hyps[1]["refuted"] and hyps[1]["refuted"][0]["uris"] == [h0["root_cause_uri"]]
    # the main graph pools every hypothesis's refutations, as before
    assert _nodes(case)[h0["root_cause_uri"]]["agent_reason"]


def test_expand_node_walks_one_hop(case, engine_run, live_tools):
    from e2ekg.explain.case import expand_node
    root = _hyp(engine_run)["root_cause_uri"]
    out = expand_node(live_tools.store, root)
    assert out["uri"] == root and not out["truncated"]
    ids = {n["id"] for n in out["nodes"]}
    assert root in ids
    assert any(e["source"] == root or e["target"] == root for e in out["edges"])
    assert all(e["source"] in ids and e["target"] in ids for e in out["edges"])
    assert any(k.startswith(("chg:", "prov:", "ops:")) for k in (e["kind"] for e in out["edges"]))
    with pytest.raises(ValueError):
        expand_node(live_tools.store, "not-a-uri")
    with pytest.raises(ValueError):
        expand_node(live_tools.store, "https://example.com/elsewhere")
