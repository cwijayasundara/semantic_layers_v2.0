"""candidate_evidence over the hero's as-of graph: the facts it reports for the changes the
model names. Expectations come from the hero story (the answer key): where each change sits
in the topology and the timeline. The tool reports; it never ranks, judges or volunteers a
candidate."""
import json

import pytest
from agent_helpers import call, copy_store

from e2ekg.agent.tools import make_tools
from e2ekg.graph.uris import change_uri
from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.variants import variant_scenarios

HERO = hero_scenario()
FAULT_VARIANTS = [s.name for s in variant_scenarios() if s.answer_key.root_cause_change_id]
VERDICT_KEYS = {"survives", "refuted_by", "supported_by", "verdict", "refuted", "passes",
                "ok", "rank", "score", "checks", "conclusion"}


def _uris(scenario, answers: dict) -> dict[str, str]:
    """scenario change id -> its node URI (through the opaque external id, R22)."""
    ext = {v: k for k, v in answers["external_ids"].items()}
    return {c.id: str(change_uri(scenario.name, c.kind, ext[c.id]))
            for c in scenario.changes if c.id in ext}


def keys(obj) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | set().union(*(keys(v) for v in obj.values()))
    if isinstance(obj, list):
        return set().union(*(keys(v) for v in obj))
    return set()


@pytest.fixture(scope="module")
def uri(live):
    return _uris(HERO, live.answers)


@pytest.fixture(scope="module")
def named(live, uri):
    order = ["chg-dep-0001", "chg-cfg-0001", "chg-rel-0001", "chg-exp-0001"]
    out = call(live.tools, "candidate_evidence", alert_id="radar-09000",
               candidate_uris=[uri[c] for c in order])
    return out, {c["uri"]: c for c in out["candidates"]}


def test_the_symptom_is_the_alert_app_from_its_rooted_onset(named):
    s = named[0]["symptom"]
    assert s["app"] == "homepage-graphql" and s["alert_at"] == "2026-10-14T22:30:00Z"
    assert s["onset"]["at"] < s["alert_at"] and s["onset"]["type"]
    assert s["filed_against"] == ["api-gateway"]


def test_candidates_come_back_in_the_order_named_and_nothing_else(named, uri):
    assert [c["uri"] for c in named[0]["candidates"]] == [
        uri["chg-dep-0001"], uri["chg-cfg-0001"], uri["chg-rel-0001"], uri["chg-exp-0001"]]


def test_no_field_is_a_verdict_or_a_ranking(named):
    assert not keys(named[0]) & VERDICT_KEYS


def test_the_root_reaches_the_alert_app_through_degraded_or_unseen_calls_before_the_onset(
        named, uri):
    f = named[1][uri["chg-cfg-0001"]]
    path = f["call_path"]
    assert path[0]["caller"] == "homepage-graphql" and path[-1]["callee"] == "title-metadata-svc"
    assert {h["status"] for h in path} <= {"degraded", "unknown"}
    assert any(h["status"] == "degraded" and h["uri"] for h in path)
    assert f["minutes_before_onset"] > 0 and f["rooted_before"]["n"] == 0
    assert f["engine_chain"]["root"] == uri["chg-cfg-0001"] and f["engine_chain"]["conf"] >= 0.6


def test_the_deploy_the_incident_was_filed_against_is_off_path_and_late(named, uri):
    f = named[1][uri["chg-dep-0001"]]
    assert f["app"] == "api-gateway" and f["call_path"] is None
    assert f["minutes_before_onset"] < 0


def test_the_client_release_is_not_something_the_alert_app_depends_on(named, uri):
    assert named[1][uri["chg-rel-0001"]]["call_path"] is None


def test_the_experiment_landed_into_degradation_the_config_change_explains(named, uri):
    f = named[1][uri["chg-exp-0001"]]
    assert f["call_path"] is not None and f["minutes_before_onset"] >= 0
    rooted = f["rooted_before"]
    assert rooted["n"] >= 1
    assert any(uri["chg-cfg-0001"] in b["rooted_in"] and b["app"] == "title-metadata-svc"
               for b in rooted["first"])


def test_autoscaling_gets_facts_and_shows_the_change_before_it(live, uri):
    # the scale-in the agent can see; the fix's scale-outs come after the cutoff
    scaling = [uri[c.id] for c in HERO.changes if c.kind == "scaling" and c.at_utc < live.as_of]
    out = call(live.tools, "candidate_evidence", alert_id="radar-09000", candidate_uris=scaling)
    for row in out["candidates"]:
        assert "error" not in row and row["call_path"] is not None
        assert uri["chg-cfg-0001"] in {c["uri"] for c in row["same_app_changes"]["first"]}


def test_an_incident_id_names_the_same_symptom(live, uri, named):
    out = call(live.tools, "candidate_evidence", alert_id="INC-5377",
               candidate_uris=[uri["chg-cfg-0001"]])
    assert out["symptom"] == named[0]["symptom"]


def test_naming_no_candidate_is_an_error_not_a_list(live):
    out = live.tools["candidate_evidence"].invoke({"alert_id": "radar-09000",
                                                   "candidate_uris": []})
    assert out.startswith("error")


def test_a_candidate_that_is_no_change_is_reported_as_such(live):
    spike = "https://kg.local/log/spike/hero/no-such-spike"
    out = call(live.tools, "candidate_evidence", alert_id="radar-09000", candidate_uris=[spike])
    assert out["candidates"] == [{"uri": spike, "error": "not a change in the graph before "
                                                        "the cutoff"}]


def test_an_unknown_alert_is_an_error(live):
    out = live.tools["candidate_evidence"].invoke({"alert_id": "radar-99999",
                                                   "candidate_uris": ["x"]})
    assert out.startswith("error")


def test_the_gate_reaches_what_the_tools_compute(live):
    open_ = make_tools(copy_store(live.store), live.proc, "hero", "nogate", llm=live.llm,
                       gate=False)
    gated = call(live.tools, "causal_chains", symptom_uri=live.symptom)
    ungated = call(open_, "causal_chains", symptom_uri=live.symptom)
    assert gated != ungated


@pytest.mark.slow
@pytest.mark.parametrize("name", FAULT_VARIANTS)
def test_every_fault_variant_root_reaches_the_alert_app_and_precedes_its_onset(
        name, harvested, scripted_llm):
    h = harvested(name, as_of=True)
    scenario = next(s for s in variant_scenarios() if s.name == name)
    uri = _uris(scenario, json.loads((h.answers / f"{name}.json").read_text()))
    tools = make_tools(copy_store(h.store), h.proc, name, "tester", llm=scripted_llm)
    root = uri[scenario.answer_key.root_cause_change_id]
    out = call(tools, "candidate_evidence", alert_id=h.symptom.rsplit("/", 1)[1],
               candidate_uris=[root, *(uri[c] for c in scenario.answer_key.red_herring_change_ids)])
    f = out["candidates"][0]
    assert f["call_path"] is not None, f
    assert f["minutes_before_onset"] is not None and f["minutes_before_onset"] >= 0, f
    assert not keys(out) & VERDICT_KEYS
