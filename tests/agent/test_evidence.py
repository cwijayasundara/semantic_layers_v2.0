"""The facts candidate_evidence reports about a change, on hand-built graphs whose answer is
known by construction (the reviewer's cx/cx2 cases). Facts only: what the topology, the clock
and the degraded calls say, never whether the change is the cause."""
from datetime import UTC, datetime, timedelta

import pytest

from e2ekg.agent.evidence import Call, Case, Observed
from e2ekg.correlate.engine import Signal

T0 = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
EDGES = [("client-web", "edge"), ("client-ios", "edge"), ("edge", "gql"),
         ("gql", "svcA"), ("svcA", "svcB"), ("svcB", "db")]
TRACED = [("gql", "svcA"), ("svcA", "svcB")]


def t(m: float, s: int = 0) -> datetime:
    return T0 + timedelta(minutes=m, seconds=s)


def sig(uri, typ, app, at, sev=0.8) -> Signal:
    return Signal(uri, typ, app, at, sev)


def story(root_at=t(0), root_type="config_change") -> list[Signal]:
    """A change on svcB degrades svcB, then svcA, then the alerting gql."""
    return [sig("chg:root", root_type, "svcB", root_at),
            sig("m:svcB", "metric_anomaly", "svcB", t(2)),
            sig("m:svcA", "slow_trace", "svcA", t(4)),
            sig("m:gql", "metric_anomaly", "gql", t(6)),
            sig("alert", "alert", "gql", t(10))]


DEGRADED = [Call("svcA", "svcB", t(3), "trc:ab"), Call("gql", "svcA", t(5), "trc:ga")]


def observed(pairs=TRACED, lo=t(-60), hi=t(30)) -> list[Observed]:
    return [Observed(a, b, lo, hi) for a, b in pairs]


def case(signals, calls=DEGRADED, seen=None, edges=EDGES, gate=True) -> Case:
    return Case(signals, edges, calls, observed() if seen is None else seen, "alert", "alert",
                gate=gate)


def facts(c: Case, uri="chg:root"):
    return c.facts(next(s for s in c.signals if s.uri == uri))


def hops(f) -> list[tuple[str, str, str]]:
    return [(h.caller, h.callee, h.status) for h in f.call_path]


def test_the_onset_is_the_first_telemetry_the_change_explains_on_the_alert_app():
    c = case(story())
    assert c.onset.uri == "m:gql"
    f = facts(c)
    assert f.minutes_before_onset == 6.0 and f.minutes_before_alert == 10.0


def test_noise_on_the_alert_app_before_any_change_does_not_move_the_onset():
    noise = [sig(f"n{i}", "log_error", "gql", t(-41 + 8 * i)) for i in range(6)]
    c = case(story() + noise)
    assert c.onset.uri == "m:gql" and facts(c).minutes_before_onset == 6.0


def test_a_change_landing_after_the_onset_has_negative_minutes():
    f = facts(case(story(root_at=t(6, 20))))
    assert f.minutes_before_onset == pytest.approx(-0.3)


def test_with_no_explained_telemetry_on_the_alert_app_the_onset_is_unknown():
    s = [sig("chg:root", "config_change", "svcB", t(7, 10)),
         sig("m:svcB", "metric_anomaly", "svcB", t(6)), sig("m:gql", "metric_anomaly", "gql", t(6)),
         sig("alert", "alert", "gql", t(10))]
    c = case(s)
    assert c.onset is None
    f = facts(c)
    assert f.minutes_before_onset is None and f.minutes_before_alert == pytest.approx(2.8)


def test_the_call_path_runs_from_the_alert_app_down_to_the_change_with_each_hop_degraded():
    f = facts(case(story()))
    assert hops(f) == [("gql", "svcA", "degraded"), ("svcA", "svcB", "degraded")]
    assert [h.signal.uri for h in f.call_path] == ["trc:ga", "trc:ab"]


def test_a_change_the_alert_app_does_not_depend_on_has_no_call_path():
    s = story() + [sig("chg:web", "deployment", "client-web", t(1))]
    assert facts(case(s), "chg:web").call_path is None


def test_a_change_on_the_alert_app_itself_has_an_empty_call_path():
    s = story() + [sig("chg:gql", "deployment", "gql", t(1))]
    assert facts(case(s), "chg:gql").call_path == ()


def test_an_untraced_hop_is_unknown_not_a_break():
    edges = [("edge", "gql"), ("gql", "svcA"), ("svcA", "q"), ("q", "svcB")]
    f = facts(case(story(), calls=[Call("gql", "svcA", t(5), "trc:ga")],
                   seen=observed([("gql", "svcA")]), edges=edges))
    assert hops(f) == [("gql", "svcA", "degraded"), ("svcA", "q", "unknown"),
                       ("q", "svcB", "unknown")]


def test_a_traced_hop_that_stayed_healthy_is_not_degraded():
    f = facts(case(story(), calls=[]))
    assert hops(f) == [("gql", "svcA", "not_degraded"), ("svcA", "svcB", "not_degraded")]


def test_a_hop_whose_spans_stop_before_the_change_is_unknown():
    f = facts(case(story(), calls=[], seen=observed(hi=t(-1))))
    assert {s for _, _, s in hops(f)} == {"unknown"}


def test_a_hop_that_degraded_before_the_change_is_not_degradation_after_it():
    early = [Call("svcA", "svcB", t(-5), "trc:early"), Call("gql", "svcA", t(5), "trc:ga")]
    f = facts(case(story(), calls=early))
    assert hops(f) == [("gql", "svcA", "degraded"), ("svcA", "svcB", "not_degraded")]


def test_the_path_prefers_degraded_hops_over_a_shorter_healthy_one():
    edges = EDGES + [("gql", "svcB")]
    f = facts(case(story(), seen=observed(TRACED + [("gql", "svcB")]), edges=edges))
    assert hops(f) == [("gql", "svcA", "degraded"), ("svcA", "svcB", "degraded")]


def test_a_blip_on_its_own_app_before_it_landed_is_unrooted_telemetry():
    f = facts(case(story() + [sig("l:svcB", "log_error", "svcB", t(-2))]))
    assert [s.uri for s in f.unrooted_before] == ["l:svcB"] and f.rooted_before == ()


def test_a_background_blip_deep_below_it_is_unrooted_telemetry():
    f = facts(case(story() + [sig("l:db", "log_error", "db", t(-8))]))
    assert [s.uri for s in f.unrooted_before] == ["l:db"] and f.rooted_before == ()


def test_degradation_an_earlier_change_explains_is_rooted_and_names_that_change():
    s = story() + [sig("chg:early", "deployment", "db", t(-9)),
                   sig("l:db", "log_error", "db", t(-8))]
    f = facts(case(s))
    assert [(b.signal.uri, b.roots) for b in f.rooted_before] == [("l:db", ("chg:early",))]
    assert f.unrooted_before == ()


def test_telemetry_on_its_callers_is_not_before_it():
    f = facts(case(story() + [sig("l:svcA", "log_error", "svcA", t(-2))]))
    assert f.unrooted_before == () and f.rooted_before == ()


def test_telemetry_more_than_the_correlation_window_before_it_is_not_reported():
    f = facts(case(story() + [sig("l:db", "log_error", "db", t(-11))]))
    assert f.unrooted_before == ()


def test_autoscaling_gets_facts_like_any_change_including_what_preceded_it():
    s = story() + [sig("chg:scale", "autoscaling", "svcB", t(1))]
    f = facts(case(s), "chg:scale")
    assert hops(f) == [("gql", "svcA", "degraded"), ("svcA", "svcB", "degraded")]
    assert [x.uri for x in f.same_app_changes] == ["chg:root"]


def test_a_commit_gets_facts_and_shows_the_deployment_that_shipped_it():
    s = story(root_type="deployment") + [sig("chg:commit", "commit", "svcB", t(-20))]
    f = facts(case(s), "chg:commit")
    assert f.call_path is not None and f.minutes_before_onset == 26.0
    assert [x.uri for x in f.same_app_changes] == ["chg:root"]


def test_the_engine_chain_is_reported_for_a_change_on_one_and_absent_otherwise():
    s = story() + [sig("chg:web", "deployment", "client-web", t(1))]
    c = case(s)
    assert facts(c).chain.links[0].uri == "chg:root" and facts(c).chain.confidence >= 0.6
    assert facts(c, "chg:web").chain is None


def test_the_gate_is_threaded_into_what_counts_as_rooted():
    s = story() + [sig("chg:side", "deployment", "side", t(-5)),
                   sig("l:db", "log_error", "db", t(-3))]
    gated, open_ = facts(case(s)), facts(case(s, gate=False))
    assert [x.uri for x in gated.unrooted_before] == ["l:db"]
    assert [(b.signal.uri, b.roots) for b in open_.rooted_before] == [("l:db", ("chg:side",))]


def test_an_alert_that_is_not_in_the_signals_is_an_error():
    with pytest.raises(ValueError):
        Case(story(), EDGES, [], [], "no-such-alert", "no-such-alert")
