"""The fast path (PROPOSAL A.2): the prefetch computes every specialist's slice with the real
tools, each specialist is one structured call whose findings are recorded exactly as
record_finding would, the synthesis is one call, and a weak synthesis escalates to the Deep
Path — which the fast path never contaminates (the Deep Path runs on a fresh store copy)."""
import json
import re

import pytest
from agent_helpers import fake_tool_model
from langchain_core.messages import AIMessage

from e2ekg.agent import fastpath
from e2ekg.agent.fastpath import (CANDIDATE_CAP, FAST_MIN_CONFIDENCE, FindingIns,
                                  _weak, brief, run_rca_fast, specialist_message,
                                  specialist_slice)
from e2ekg.agent.report import Hypothesis, RCAReport
from e2ekg.agent.specialists import SPECIALISTS, prompt
from e2ekg.agent.trail import Trail

ALERT, ALERT_APP = "radar-09000", "homepage-graphql"


@pytest.fixture(scope="module")
def fast_root(tmp_path_factory, harvested_hero, harvested_hero_as_of, preprocessed_hero):
    """A data root laid out like data/: the as-of store, beside it the full store and the raw
    tree — everything run_rca_fast may open, and the full store it must not."""
    root = tmp_path_factory.mktemp("data")
    for link, target in {"raw/hero": preprocessed_hero.raw,
                         "processed/hero": preprocessed_hero.proc,
                         "graph/hero/store": harvested_hero.out / "store",
                         "graph/hero/as_of": harvested_hero_as_of.out}.items():
        (root / link).parent.mkdir(parents=True, exist_ok=True)
        (root / link).symlink_to(target)
    return root


@pytest.fixture(scope="module")
def batch(fast_root, harvested_hero_as_of, preprocessed_hero):
    return specialist_slice(harvested_hero_as_of.store, preprocessed_hero.proc, "hero", ALERT)


def _call(name: str, args: dict, i: int = 0) -> dict:
    return {"name": name, "args": args, "id": f"{name}-{i}"}


# --- the prefetch -------------------------------------------------------------------------


def test_the_slice_carries_every_specialists_tools(batch):
    assert set(batch["slices"]) == set(SPECIALISTS)
    assert set(batch["slices"]["topology"]) == {"dependency_closure (direction down)",
                                                "dependency_closure (direction up)", "trace_edges"}
    assert set(batch["slices"]["change"]) == {"changes_in_window"}
    assert set(batch["slices"]["metrics"]) == {"anomalies_in_window"}
    assert set(batch["slices"]["logs"]) == {"log_spikes"}
    assert set(batch["slices"]["traces"]) == {"trace_edges"}
    assert set(batch["slices"]["alerts_incidents"]) == {"find_symptom", "alerts_incidents"}
    assert set(batch["slices"]["history"]) == {"similar_incidents", "chat_claims"}
    assert "INC-5377" in batch["slices"]["history"]["chat_claims"]   # the incident as the channel


def test_no_prefetched_tool_failed(batch):
    for answers in batch["slices"].values():
        for tool, answer in answers.items():
            assert not answer.startswith("error: "), f"{tool}: {answer[:80]}"


def test_the_candidate_facts_cover_the_windows_changes(batch):
    uris = [r["uri"] for r in json.loads(batch["changes"]) if r.get("uri")]
    assert uris and len(uris) <= CANDIDATE_CAP
    assert batch["facts"] is not None
    assert {c["uri"] for c in batch["facts"]["candidates"] if "uri" in c} == set(uris)
    assert batch["facts"]["symptom"]["alert"]


def test_the_brief_names_the_alert_the_symptom_and_the_window(batch):
    b = brief(ALERT, batch["sym"], batch["start"])
    assert ALERT in b and batch["sym"]["symptom_uri"] in b and batch["sym"]["as_of"] in b
    assert "INC-5377" in b          # what the incident was filed against, as a hypothesis


def test_the_specialist_message_carries_the_slice_and_the_reporting_contract(batch):
    msg = specialist_message("logs", brief(ALERT, batch["sym"], batch["start"]),
                             batch["slices"]["logs"])
    assert "one-shot" in msg and "- log_spikes: " in msg
    assert "FindingIns" in msg and "record_finding validates" in msg
    # and the system prompt stays the specialist's own, so the domain judgment is unchanged
    assert "logs specialist" in prompt("logs")


# --- a scripted fast run ------------------------------------------------------------------

MARKS = [("dependency_closure", "topology"), ("anomalies_in_window", "metrics"),
         ("log_spikes", "logs"), ("alerts_incidents:", "alerts_incidents"),
         ("similar_incidents", "history"), ("changes_in_window", "change"),
         ("trace_edges", "traces")]          # traces last: the topology slice has trace_edges too


def fast_policy(synthesis: dict, transcript: list):
    """One policy for every fast-path call: each specialist cites the first change its slice
    shows; the synthesis weighs what it is given and returns `synthesis`. The specialists see
    {FindingIns} bound, the synthesis {RCAReport}."""
    def policy(messages, bound):
        text = "\n".join(str(m.content) for m in messages)
        if set(bound) == {"RCAReport"}:
            transcript.append(("synthesis", text))
            return AIMessage("", tool_calls=[_call("RCAReport", synthesis)])
        name = next(n for mark, n in MARKS if mark in text)
        transcript.append((name, text))
        change = next((u for u in re.findall(r'"uri":\s*"([^"]+/chg/[^"]+)"', text)), None)
        findings = [] if change is None else [{
            "about_app": ALERT_APP, "claim": f"{name} saw {change}",
            "signal_uris": [change], "confidence": 0.5}]
        return AIMessage("", tool_calls=[_call("FindingIns", {"findings": findings})])
    return policy


def test_the_fast_run_records_findings_and_returns_the_synthesis(fast_root):
    transcript, trail = [], Trail()
    report = run_rca_fast(ALERT, "hero", root=fast_root, trail=trail,
                          model=fake_tool_model(fast_policy({
                              "verdict": "root_cause_found",
                              "hypotheses": [{"root_cause_uri": "u", "chain": ["u"],
                                              "confidence": 0.9, "evidence": ["u"],
                                              "refuted_alternatives": []}],
                              "summary": "fast scripted"}, transcript)))
    assert report.verdict == "root_cause_found" and report.summary == "fast scripted"
    tools = [s["tool"] for s in trail.steps]
    assert tools[0] == "fast:prefetch"
    assert {t for t in tools if t.startswith("fast:")} == {"fast:prefetch", "fast:findings",
                                                           *{f"fast:{n}" for n in SPECIALISTS}}
    assert tools.count("record_finding") >= 1
    assert "escalate" not in tools
    assert trail.findings and all(f["specialist"] in SPECIALISTS for f in trail.findings)
    # each specialist's message carried its own prompt (the system side) and its slice
    for name, text in transcript:
        if name in SPECIALISTS:
            assert prompt(name) in text and "one-shot" in text
    claims = {s["args"]["claim"] for s in trail.steps if s["tool"] == "record_finding"}
    assert claims and claims <= {f["claim"] for f in trail.findings}


def test_a_weak_synthesis_escalates_to_the_deep_path(monkeypatch, fast_root):
    deep = {"calls": 0, "kwargs": None}

    def fake_run_rca(alert_id, scenario, **kw):
        deep["calls"] += 1
        deep["kwargs"] = {"alert": alert_id, **kw}
        return RCAReport(verdict="root_cause_found", hypotheses=[], summary="deep scripted")

    monkeypatch.setattr(fastpath, "run_rca", fake_run_rca)
    trail = Trail()
    report = run_rca_fast(ALERT, "hero", root=fast_root, trail=trail, model=fake_tool_model(
        fast_policy({"verdict": "root_cause_found",
                     "hypotheses": [{"root_cause_uri": "u", "chain": ["u"], "confidence": 0.3,
                                     "evidence": ["u"], "refuted_alternatives": []}],
                     "summary": "weak"}, [])))
    assert report.summary == "deep scripted" and deep["calls"] == 1
    assert deep["kwargs"]["alert"] == ALERT and deep["kwargs"]["trail"] is trail
    step = next(s for s in trail.steps if s["tool"] == "escalate")
    assert f"{0.3:.2f} < {FAST_MIN_CONFIDENCE}" in step["args"]["reason"]


def test_a_failed_prefetch_escalates(monkeypatch, fast_root):
    monkeypatch.setattr(fastpath, "run_rca",
                        lambda *a, **k: RCAReport(verdict="inconclusive", hypotheses=[],
                                                  summary="deep scripted"))
    report = run_rca_fast("no-such-alert", "hero", root=fast_root)
    assert report.summary == "deep scripted"


def test_escalate_false_keeps_the_fast_failure(fast_root):
    report = run_rca_fast("no-such-alert", "hero", root=fast_root, escalate=False)
    assert report.verdict == "inconclusive" and "prefetch failed" in report.failure


# --- the weakness rules -------------------------------------------------------------------

def _report(*confs_roots) -> RCAReport:
    return RCAReport(verdict="root_cause_found",
                     hypotheses=[Hypothesis(root_cause_uri=r, chain=[r], confidence=c,
                                            evidence=[], refuted_alternatives=[])
                                 for c, r in confs_roots], summary="")


def test_a_strong_single_hypothesis_stands():
    assert _weak(_report((0.9, "u"))) is None


def test_a_low_confidence_escalates():
    assert "confidence" in _weak(_report((FAST_MIN_CONFIDENCE - 0.01, "u")))


def test_a_top_two_tie_escalates():
    why = _weak(_report((0.7, "a"), (0.66, "b")))
    assert why and "tie" in why


def test_a_clear_winner_by_more_than_the_tie_stands():
    assert _weak(_report((0.7, "a"), (0.5, "b"))) is None


def test_an_inconclusive_synthesis_escalates():
    report = RCAReport(verdict="inconclusive", hypotheses=[], summary="")
    why = _weak(report)
    assert why and "inconclusive" in why


def test_the_findings_ins_defaults_to_nothing():
    assert FindingIns().findings == []
