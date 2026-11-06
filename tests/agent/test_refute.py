"""refutation_check: the tests a candidate root cause must survive, computed from the as-of
graph. Expectations come from the answer key: the root survives and ranks first, every red
herring is refuted, and each for the reason its place in the topology and timeline gives."""
import json

import pytest
from agent_helpers import call, copy_store

from e2ekg.agent.tools import make_tools
from e2ekg.graph.uris import change_uri
from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.variants import variant_scenarios

HERO = hero_scenario()
FAULT_VARIANTS = [s.name for s in variant_scenarios() if s.answer_key.root_cause_change_id]


def _uris(scenario, answers: dict) -> dict[str, str]:
    """scenario change id -> its node URI (through the opaque external id, R22)."""
    ext = {v: k for k, v in answers["external_ids"].items()}
    return {c.id: str(change_uri(scenario.name, c.kind, ext[c.id]))
            for c in scenario.changes if c.id in ext}


@pytest.fixture(scope="module")
def checked(live):
    out = call(live.tools, "refutation_check", alert_id="radar-09000", candidate_uris=[])
    return out, _uris(HERO, live.answers)


def _row(out, uri):
    return next(c for c in out["candidates"] if c["uri"] == uri)


def test_the_root_cause_survives_and_ranks_first(checked):
    out, uri = checked
    top = out["candidates"][0]
    assert top["uri"] == uri["chg-cfg-0001"] and top["survives"] is True
    assert top["refuted_by"] == [] and top["chain_conf"] >= 0.6


def test_nothing_else_survives(checked):
    out, uri = checked
    assert [c["uri"] for c in out["candidates"] if c["survives"]] == [uri["chg-cfg-0001"]]


@pytest.mark.parametrize("herring,check", [("chg-dep-0001", "closure"),
                                           ("chg-rel-0001", "closure"),
                                           ("chg-rel-0001", "clients"),
                                           ("chg-exp-0001", "already_degrading")])
def test_each_red_herring_is_refuted_for_its_reason(checked, herring, check):
    out, uri = checked
    row = _row(out, uri[herring])
    assert row["survives"] is False
    assert any(r.startswith(f"{check}:") for r in row["refuted_by"]), row


def test_autoscaling_is_a_reaction_not_an_origin(live, checked):
    uri = checked[1]
    scaling = [uri[c.id] for c in HERO.changes if c.kind == "scaling"]
    out = call(live.tools, "refutation_check", alert_id="radar-09000", candidate_uris=scaling)
    assert all(any(r.startswith("origin:") for r in _row(out, u)["refuted_by"])
               for u in scaling)


def test_the_symptom_is_the_alert_app_degrading_not_the_app_the_incident_was_filed_against(
        checked):
    s = checked[0]["symptom"]
    assert s["app"] == "homepage-graphql" and s["onset"] < s["alert_at"] == "2026-10-14T22:30:00Z"
    assert s["clients"] == ["client-android", "client-ios", "client-tv", "client-web"]
    assert s["filed_against"] == ["api-gateway"] and s["filed_in_closure"] is False


def test_a_candidate_the_model_names_is_judged_too(live, checked):
    uri = checked[1]["chg-dep-0001"]
    out = call(live.tools, "refutation_check", alert_id="INC-5377", candidate_uris=[uri])
    assert _row(out, uri)["survives"] is False


def test_a_candidate_that_is_no_change_is_reported_not_judged(live):
    spike = "https://kg.local/log/spike/hero/no-such-spike"
    out = call(live.tools, "refutation_check", alert_id="radar-09000", candidate_uris=[spike])
    assert "error" in _row(out, spike)


def test_an_unknown_alert_is_an_error(live):
    out = live.tools["refutation_check"].invoke({"alert_id": "radar-99999",
                                                 "candidate_uris": []})
    assert out.startswith("error")


@pytest.mark.slow
@pytest.mark.parametrize("name", FAULT_VARIANTS)
def test_every_fault_variant_root_survives_and_its_herrings_do_not(name, harvested,
                                                                  scripted_llm):
    h = harvested(name, as_of=True)
    scenario = next(s for s in variant_scenarios() if s.name == name)
    answers = json.loads((h.answers / f"{name}.json").read_text())
    tools = make_tools(copy_store(h.store), h.proc, name, "tester", llm=scripted_llm)
    uri = _uris(scenario, answers)
    herrings = scenario.answer_key.red_herring_change_ids
    out = call(tools, "refutation_check", alert_id=h.symptom.rsplit("/", 1)[1],
               candidate_uris=[uri[c] for c in herrings])
    top = out["candidates"][0]
    assert top["uri"] == uri[scenario.answer_key.root_cause_change_id] and top["survives"]
    for herring in herrings:
        assert not _row(out, uri[herring])["survives"], herring
