"""Scoring a report against the answer key. The key names changes by scenario id; reports
name them by opaque external id, so these tests build reports the way the agents do."""
import json

import pytest

from e2ekg.agent.report import Hypothesis, RCAReport
from e2ekg.evals.score import evidence_checker, score, scenario_change, true_root_rank
from e2ekg.graph.uris import change_uri

ANSWERS = {
    "answer_key": {"root_cause_change_id": "chg-cfg-0001",
                   "red_herring_change_ids": ["chg-dep-0001", "chg-exp-0001"]},
    "external_ids": {"fpc-3be29b76c7f9": "chg-cfg-0001", "01J239A03CBAD72F638D493134": "chg-dep-0001",
                     "alloc-8ba292395c": "chg-exp-0001", "fpc-01a317a3dd3c": "chg-bg-0001"},
    "evidence_raw_ids": ["events|events/properties.log|10|20|aaaa",
                         "logs|logs/svc/i-1.log|0|5|bbbb"],
}
CONTROL = {"answer_key": {"root_cause_change_id": None,
                          "red_herring_change_ids": ["chg-dep-0001"]},
           "external_ids": ANSWERS["external_ids"], "evidence_raw_ids": []}
EXT = {v: k for k, v in ANSWERS["external_ids"].items()}


def uri(cid: str, kind: str = "config_change") -> str:
    return str(change_uri("hero", kind, EXT[cid]))


def report(*roots: str, verdict="root_cause_found", evidence=()) -> RCAReport:
    return RCAReport(verdict=verdict, summary="s", hypotheses=[
        Hypothesis(root_cause_uri=r, chain=[r], confidence=0.8, evidence=list(evidence),
                   refuted_alternatives=[]) for r in roots])


def test_the_true_root_first_is_top1_and_top3_and_no_trap():
    s = score(report(uri("chg-cfg-0001")), ANSWERS)
    assert (s.top1, s.top3, s.trap, s.control_false_positive) == (True, True, False, False)


def test_a_red_herring_first_is_a_trap_and_the_root_second_is_top3_only():
    s = score(report(uri("chg-dep-0001", "deployment"), uri("chg-cfg-0001")), ANSWERS)
    assert (s.top1, s.top3, s.trap) == (False, True, True)


def test_the_root_fourth_is_neither_top1_nor_top3():
    r = report(*[uri(c, k) for c, k in (("chg-dep-0001", "deployment"),
                                        ("chg-exp-0001", "experiment_allocation"),
                                        ("chg-bg-0001", "config_change"),
                                        ("chg-cfg-0001", "config_change"))])
    s = score(r, ANSWERS)
    assert (s.top1, s.top3) == (False, False) and true_root_rank(r, ANSWERS) == 4


def test_a_wrong_root_that_is_not_a_herring_is_no_trap():
    assert not score(report(uri("chg-bg-0001")), ANSWERS).trap


def test_an_inconclusive_report_names_no_root_even_if_it_lists_one():
    s = score(report(uri("chg-cfg-0001"), verdict="inconclusive"), ANSWERS)
    assert (s.top1, s.top3, s.trap) == (False, False, False)


def test_a_bare_external_id_or_one_inside_text_names_the_change():
    assert scenario_change("fpc-3be29b76c7f9", ANSWERS) == "chg-cfg-0001"
    assert scenario_change("config change fpc-3be29b76c7f9 by v.rao", ANSWERS) == "chg-cfg-0001"
    assert scenario_change("https://kg.local/app/application/title-metadata-svc", ANSWERS) is None
    assert scenario_change("fpc-3be29b76c7f9 and 01J239A03CBAD72F638D493134", ANSWERS) is None


def test_a_scenario_change_id_names_nothing():
    assert scenario_change("chg-cfg-0001", ANSWERS) is None


def test_control_false_positive_is_a_found_root_where_there_is_none():
    assert score(report(uri("chg-bg-0001")), CONTROL).control_false_positive
    s = score(report(uri("chg-dep-0001", "deployment")), CONTROL)
    assert s.control_false_positive and s.trap


def test_the_control_is_scored_right_when_the_report_abstains():
    s = score(report(verdict="inconclusive"), CONTROL)
    assert (s.top1, s.top3, s.control_false_positive) == (True, True, False)
    assert not score(report(uri("chg-bg-0001")), CONTROL).top1


def test_a_fault_scenario_is_never_a_control_false_positive():
    assert not score(report(uri("chg-bg-0001")), ANSWERS).control_false_positive


def test_evidence_precision_is_the_share_of_distinct_cited_items_that_reach_the_key():
    r = report(uri("chg-cfg-0001"), evidence=["events|events/properties.log|10|20|aaaa",
                                              "events|events/properties.log|10|20|aaaa",
                                              "https://kg.local/x", "free text"])
    assert score(r, ANSWERS).evidence_precision == pytest.approx(1 / 3)
    assert score(report(uri("chg-cfg-0001")), ANSWERS).evidence_precision == 0.0
    assert score(report(verdict="inconclusive"), ANSWERS).evidence_precision == 0.0


def test_a_failed_run_scores_as_a_miss_not_a_crash():
    from e2ekg.agent.autorca import FailedRCA
    s = score(FailedRCA(verdict="inconclusive", hypotheses=[], summary="x", failure="y"), ANSWERS)
    assert (s.top1, s.top3, s.trap, s.control_false_positive) == (False,) * 4
    failed_control = score(FailedRCA(verdict="inconclusive", hypotheses=[], summary="x",
                                     failure="y"), CONTROL)
    assert not failed_control.top1 and not failed_control.control_false_positive


def test_the_real_hero_key_maps_through_external_ids(preprocessed_hero):
    answers = json.loads((preprocessed_hero.answers / "hero.json").read_text())
    ext = {v: k for k, v in answers["external_ids"].items()}
    root = str(change_uri("hero", "config_change", ext["chg-cfg-0001"]))
    herring = str(change_uri("hero", "deployment", ext["chg-dep-0001"]))
    assert score(report(root), answers).top1
    assert score(report(herring), answers).trap


@pytest.fixture
def raw_tree(tmp_path):
    """events/properties.log: lines aa bb cc dd ee at bytes 0, 3, 6, 9, 12; the key's evidence
    is the record `cc`. The view drops `aa`, so its line 2 is the original line 3."""
    (tmp_path / "events").mkdir()
    (tmp_path / "events/properties.log").write_bytes(b"aa\nbb\ncc\ndd\nee\n")
    view = tmp_path / "view/events"
    view.mkdir(parents=True)
    (view / "properties.log").write_bytes(b"bb\ncc\ndd\nee\n")
    return tmp_path


KEY = {"evidence_raw_ids": ["events|events/properties.log|6|3|cccc"]}


def test_a_line_citation_hits_the_key_record_in_the_original_tree(raw_tree):
    hit = evidence_checker(KEY, raw=raw_tree)
    assert hit("events/properties.log:3") and hit("/raw/events/properties.log:2-3")
    assert not hit("events/properties.log:2") and not hit("events/properties.log:9")
    assert not hit("events/missing.log:3")


def test_a_line_citation_in_a_view_is_mapped_back_to_original_bytes(raw_tree):
    view = {"events/properties.log": [[0, 3, 12]]}
    hit = evidence_checker(KEY, raw=raw_tree / "view", view=view)
    assert hit("events/properties.log:2") and not hit("events/properties.log:1")
    assert hit("events/properties.log:1-2") and not hit("events/properties.log:3-4")


def test_a_view_with_a_gap_maps_each_segment_to_its_own_original_offsets(raw_tree):
    (raw_tree / "view/events/properties.log").write_bytes(b"aa\ncc\n")
    view = {"events/properties.log": [[0, 0, 3], [3, 6, 3]]}
    hit = evidence_checker(KEY, raw=raw_tree / "view", view=view)
    assert hit("events/properties.log:2") and not hit("events/properties.log:1")
