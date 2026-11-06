from datetime import timedelta

from helpers import hero_fault, hero_window, rng

from e2ekg.scenario.model import Fault
from e2ekg.scenario.propagation import ONSET_DELAY_S, propagate
from e2ekg.scenario.topology import downstream_closure, hero_catalog, hero_edges
from e2ekg.timeutil import pt


def run(fault=None):
    e = hero_edges(hero_catalog(rng(7)))
    return e, propagate(fault or hero_fault(), e, hero_window())


def test_propagation_reaches_all_clients_after_pool_exhaustion():
    _, d = run()
    pool = next(x for x in d if x.kind == "pool_exhaustion")
    assert pool.app == "title-batch-loader"
    clients = [x for x in d if x.app.startswith("client-") and x.kind == "latency"]
    assert {c.app for c in clients} == {"client-tv", "client-android",
                                        "client-ios", "client-web"}
    assert all(c.start_utc > pool.start_utc for c in clients)
    assert all(abs(c.magnitude - 1.5) < 1e-9 for c in clients)


def test_clients_start_exactly_1520_pt():
    _, d = run()
    for x in d:
        if x.app.startswith("client-"):
            assert x.start_utc == pt(2026, 10, 14, 15, 20)


def test_exact_schedule():
    _, d = run()
    onset = pt(2026, 10, 14, 15, 5)
    got = {(x.app, x.kind): (x.start_utc - onset).total_seconds() for x in d}
    assert got[("title-metadata-svc", "latency")] == 0
    assert got[("title-batch-loader", "pool_exhaustion")] == 180
    assert got[("row-ranker", "timeouts")] == 330
    assert got[("lolomo-svc", "timeouts")] == 510
    assert got[("homepage-graphql", "partial_response")] == 630
    assert got[("edge-gateway", "timeouts")] == 780
    assert got[("edge-gateway", "load_shed")] == 960
    assert len(d) == 11  # fault + 5 mid + 4 clients + load_shed


def test_every_degradation_traces_to_fault_through_edges():
    e, d = run()
    for x in d:
        assert "title-metadata-svc" in downstream_closure(e, x.app) | {x.app}
    assert "api-gateway" not in {x.app for x in d}


def test_none_fault_and_other_kinds():
    f = Fault(kind="none", target=None, start_utc=pt(2026, 10, 14, 14, 0),
              trigger_change_id=None)
    assert run(f)[1] == ()
    assert set(ONSET_DELAY_S) == {
        "capacity_reduction", "bad_deploy", "cache_eviction_storm",
        "db_hot_partition", "cert_expiry", "retry_storm", "none"}
    assert ONSET_DELAY_S["capacity_reduction"] == 780


def test_deterministic_and_within_window():
    assert run()[1] == run()[1]
    w = hero_window()
    assert all(w.start_utc <= x.start_utc < w.end_utc for x in run()[1])
    assert timedelta(0) < hero_window().end_utc - hero_window().start_utc
