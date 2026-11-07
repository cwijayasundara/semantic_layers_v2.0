from e2ekg.scenario.model import SIGNAL_TYPES
from e2ekg.scenario.propagation import propagate
from e2ekg.scenario.variants import variant_scenarios


def V():
    return {s.name: s for s in variant_scenarios()}


def test_six_named_variants():
    assert list(V()) == ["v-bad-deploy", "v-cache-storm", "v-hot-partition",
                         "v-cert-expiry", "v-retry-storm", "v-control"]


def test_control_variant_has_no_root_cause():
    c = V()["v-control"]
    assert c.answer_key.root_cause_change_id is None
    assert c.fault.kind == "none" and c.fault.target is None and c.fault.trigger_change_id is None
    assert c.answer_key.causal_chain == ()
    assert len(c.answer_key.red_herring_change_ids) >= 2
    assert len(c.changes) >= 4 and not any(x.is_root_cause for x in c.changes)


def test_targets_and_triggers():
    v = V()
    assert (v["v-bad-deploy"].fault.target, v["v-bad-deploy"].fault.kind) == ("lolomo-svc", "bad_deploy")
    assert v["v-cache-storm"].fault.target == "evcache-titles"
    assert v["v-hot-partition"].fault.target == "cassandra-titles"
    assert v["v-cert-expiry"].fault.target == "entitlement-svc"
    assert v["v-retry-storm"].fault.target == "row-ranker"
    kinds = {n: next(c for c in s.changes if c.id == s.answer_key.root_cause_change_id).kind
             for n, s in v.items() if n != "v-control"}
    assert kinds["v-bad-deploy"] == "deployment" and kinds["v-cache-storm"] == "config_change"
    assert kinds["v-cert-expiry"] == "config_change" and kinds["v-retry-storm"] == "deployment"
    assert kinds["v-hot-partition"] in ("commit", "config_change")


def test_every_variant_well_formed():
    for n, s in V().items():
        ak = s.answer_key
        assert len(ak.red_herring_change_ids) >= 2, n
        ids = {c.id for c in s.changes}
        assert len(ids) == len(s.changes)
        assert set(ak.red_herring_change_ids) <= ids and ak.root_cause_change_id not in ak.red_herring_change_ids
        assert all(l.signal in SIGNAL_TYPES for l in ak.causal_chain)
        assert all(s.window.start_utc <= c.at_utc < s.window.end_utc for c in s.changes), n
        assert len(s.history) == 30
        if n == "v-control":
            continue
        rc = [c for c in s.changes if c.is_root_cause]
        assert [c.id for c in rc] == [ak.root_cause_change_id] == [s.fault.trigger_change_id]
        assert rc[0].at_utc == s.fault.start_utc
        degs = propagate(s.fault, s.edges, s.window)
        assert degs
        chain_times = {(l.signal, l.app): l.at_utc for l in ak.causal_chain}
        assert chain_times[("metric_anomaly", s.fault.target)] == degs[0].start_utc
        assert ak.causal_chain[-1].signal == "incident" and ak.causal_chain[-2].signal == "alert"
        assert [l.at_utc for l in ak.causal_chain] == sorted(l.at_utc for l in ak.causal_chain)


def test_retry_storm_chain_has_commit_then_deploy():
    s = V()["v-retry-storm"]
    sig = [l.signal for l in s.answer_key.causal_chain]
    assert sig[:2] == ["commit", "deployment"]
    assert next(c for c in s.changes if c.kind == "commit").at_utc < s.fault.start_utc


def test_same_world_and_deterministic():
    from e2ekg.scenario.hero import hero_scenario
    h = hero_scenario()
    assert all(s.catalog == h.catalog and s.edges == h.edges for s in V().values())
    assert [s.model_dump_json() for s in variant_scenarios(7)] == [s.model_dump_json() for s in variant_scenarios(7)]


import pytest  # noqa: E402

from e2ekg.scenario.hero import hero_scenario as _hero  # noqa: E402
from e2ekg.scenario.topology import upstream_closure  # noqa: E402

_ALL = (_hero(),) + variant_scenarios()


@pytest.mark.parametrize("s", _ALL, ids=lambda s: s.name)
def test_chain_gaps_and_topology(s):
    ch = s.answer_key.causal_chain
    for a, b in zip(ch, ch[1:]):
        gap = (b.at_utc - a.at_utc).total_seconds()
        assert 0 <= gap <= 600, (s.name, a, b)
        assert (a.app == b.app or b.signal in ("alert", "incident")
                or b.app in upstream_closure(s.edges, a.app)), (s.name, a, b)
