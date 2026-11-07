"""The investigation trail: an ordered tool-call log and the findings, taken from the private
store copy before it is discarded."""
import json

import pytest
from agent_helpers import fake_tool_model
from test_autorca import ALERT, hero_root, investigator  # noqa: F401  (hero_root is a fixture)

from e2ekg.agent.autorca import run_rca
from e2ekg.agent.trail import Trail


@pytest.fixture(scope="module")
def traced(hero_root, scripted_llm, harvested_hero_as_of):  # noqa: F811
    before = harvested_hero_as_of.store.quad_count()
    trail = Trail()
    report = run_rca(ALERT, "hero", root=hero_root, llm=scripted_llm, trail=trail,
                     model=fake_tool_model(investigator({}, [])),
                     evidence_root=hero_root / "agent" / "hero")
    return {"trail": trail, "report": report, "before": before}


def test_trail_has_ordered_tool_steps(traced):
    steps = traced["trail"].steps
    assert steps[0]["tool"] == "find_symptom"
    assert [s["i"] for s in steps] == sorted({s["i"] for s in steps})
    assert all("output" in s and isinstance(s["args"], dict) and s["at"].endswith("+00:00")
               for s in steps)
    assert all(len(s["output"]) <= 2000 for s in steps)


def test_trail_findings_match_record_finding_calls(traced):
    trail = traced["trail"]
    recorded = [s for s in trail.steps if s["tool"] == "record_finding" and '"uri"' in s["output"]]
    assert recorded and len(recorded) == len(trail.findings)
    assert all(f["cites"] and f["specialist"] and f["app"] and f["claim"] for f in trail.findings)
    assert [f["uri"] for f in trail.findings] == sorted(f["uri"] for f in trail.findings)
    assert json.loads(json.dumps(trail.dump())) == trail.dump()


def test_store_on_disk_untouched(traced, harvested_hero_as_of):
    assert harvested_hero_as_of.store.quad_count() == traced["before"]
