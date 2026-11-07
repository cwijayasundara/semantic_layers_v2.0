from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.model import CHANGE_SIGNAL, SIGNAL_TYPES
from e2ekg.scenario.propagation import propagate
from e2ekg.timeutil import pt


def change(s, cid):
    return next(c for c in s.changes if c.id == cid)


def test_hero_answer_key():
    s = hero_scenario()
    assert s.answer_key.root_cause_change_id == "chg-cfg-0001"
    assert s.fault.trigger_change_id == "chg-cfg-0001"
    assert set(s.answer_key.red_herring_change_ids) == {"chg-exp-0001", "chg-rel-0001", "chg-dep-0001"}


def test_root_cause_outside_ten_minute_window_of_symptom():
    s = hero_scenario()
    cfg = change(s, "chg-cfg-0001")
    assert cfg.is_root_cause
    assert cfg.at_utc == pt(2026, 10, 14, 14, 52)
    assert (pt(2026, 10, 14, 15, 20) - cfg.at_utc).total_seconds() > 600


def test_red_herrings_inside_ten_minute_window():
    s = hero_scenario()
    for cid in ("chg-rel-0001", "chg-dep-0001"):
        assert (pt(2026, 10, 14, 15, 20) - change(s, cid).at_utc).total_seconds() <= 600
    assert change(s, "chg-exp-0001").at_utc == pt(2026, 10, 14, 15, 5)


def test_scaling_events_are_effects_not_herrings():
    s = hero_scenario()
    asg = [c for c in s.changes if c.id.startswith("chg-asg-")]
    assert [c.id for c in asg] == [f"chg-asg-000{i}" for i in range(1, 7)]
    assert [c.at_utc for c in asg] == [pt(2026, 10, 14, 15, m) for m in (0, 2, 4, 6, 8, 10)]
    assert asg[-1].attrs["desired_capacity"] == "12"
    assert not any(c.is_root_cause for c in asg)
    assert not set(s.answer_key.red_herring_change_ids) & {c.id for c in asg}


def test_only_one_root_cause_and_background_changes():
    s = hero_scenario()
    assert [c.id for c in s.changes if c.is_root_cause] == ["chg-cfg-0001"]
    bg = [c for c in s.changes if c.id.startswith("chg-bg-")]
    assert len(bg) >= 5
    assert any(0 < (pt(2026, 10, 14, 15, 20) - c.at_utc).total_seconds() <= 600 for c in bg)
    assert all(s.window.start_utc <= c.at_utc < s.window.end_utc for c in s.changes)
    chain_apps = {l.app for l in s.answer_key.causal_chain}
    assert not any(c.app in chain_apps for c in bg)


def test_causal_chain():
    s = hero_scenario()
    ch = s.answer_key.causal_chain
    assert [(l.signal, l.app, l.at_utc) for l in ch] == [
        ("config_change", "title-metadata-svc", pt(2026, 10, 14, 14, 52)),
        ("autoscaling", "title-metadata-svc", pt(2026, 10, 14, 15, 0)),
        ("metric_anomaly", "title-metadata-svc", pt(2026, 10, 14, 15, 5)),
        ("log_error", "title-batch-loader", pt(2026, 10, 14, 15, 8)),
        ("slow_trace", "lolomo-svc", pt(2026, 10, 14, 15, 13, 30)),
        ("metric_anomaly", "client-tv", pt(2026, 10, 14, 15, 20)),
        ("alert", "homepage-graphql", pt(2026, 10, 14, 15, 30)),
        ("incident", "homepage-graphql", pt(2026, 10, 14, 15, 34)),
    ]
    assert all(l.signal in SIGNAL_TYPES for l in ch)
    assert s.answer_key.evidence_tags == (
        "evidence:config_change", "evidence:autoscaling", "evidence:metric_anomaly",
        "evidence:log_error", "evidence:slow_trace", "evidence:alert", "evidence:incident")


def test_chain_times_match_propagation():
    s = hero_scenario()
    degs = {(d.app, d.kind): d.start_utc for d in propagate(s.fault, s.edges, s.window)}
    assert degs[("title-metadata-svc", "latency")] == pt(2026, 10, 14, 15, 5)
    assert degs[("client-tv", "latency")] == pt(2026, 10, 14, 15, 20)
    assert s.fault.start_utc == pt(2026, 10, 14, 14, 52)
    assert s.window.start_utc == pt(2026, 10, 14, 12, 0)
    assert s.window.end_utc == pt(2026, 10, 14, 18, 0)


def test_change_kinds_map_to_signals():
    assert CHANGE_SIGNAL["scaling"] == "autoscaling"


def test_history_priors():
    h = hero_scenario().history
    sev2 = [i for i in h if i.severity == "SEV2"]
    gw = [i for i in sev2 if i.cause_app == "api-gateway" and i.cause_change_kind == "deployment"]
    assert len(h) == 30 and len(sev2) == 25 and len(gw) == 10
    assert sum("Connection Timeout" in i.summary for i in gw) == 3
    assert sum("Connection Timeout" in i.summary for i in h) == 3


def test_history_has_last_quarter_twin():
    from e2ekg.scenario.history import TWIN_RESOLVER
    s = hero_scenario()
    twins = [i for i in s.history if i.cause_app == "title-metadata-svc" and i.cause_change_kind == "config_change"]
    assert len(twins) == 1
    t = twins[0]
    assert t.severity == "SEV2" and t.affected == "lolomo-svc"
    assert t.resolver_handle == TWIN_RESOLVER
    days = (s.window.start_utc - t.opened_utc).total_seconds() / 86400
    assert 75 <= days <= 105
    assert sum(i.resolver_handle == TWIN_RESOLVER for i in s.history) == 1


def test_deterministic():
    assert hero_scenario(7).model_dump_json() == hero_scenario(7).model_dump_json()
