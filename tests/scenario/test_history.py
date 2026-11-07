from e2ekg.scenario.history import build_history
from e2ekg.scenario.topology import hero_catalog, hero_edges
from e2ekg.timeutil import pt
from helpers import rng


def _h(seed=3):
    cat = hero_catalog(rng(7))
    return build_history(cat, hero_edges(cat), rng(seed), 90, pt(2026, 10, 14, 12, 0)), cat


def test_counts_and_determinism():
    h, _ = _h()
    assert len(h) == 30 and sum(i.severity == "SEV2" for i in h) == 25
    assert h == _h()[0]
    assert len({i.id for i in h}) == 30


def test_apps_in_catalog_and_sorted_by_time():
    h, cat = _h()
    ids = {a.id for a in cat}
    for i in h:
        assert i.affected in ids and (i.cause_app is None or i.cause_app in ids)
    assert [i.opened_utc for i in h] == sorted(i.opened_utc for i in h)


def test_non_twin_incidents_span_the_window_and_vary():
    end = pt(2026, 10, 14, 12, 0)
    h, _ = _h()
    rest = [i for i in h if i.cause_app != "title-metadata-svc"]
    assert all(0 < (end - i.opened_utc).days <= 90 for i in rest)
    assert len({i.resolver_handle for i in h}) >= 6
    assert len({i.cause_app for i in h}) >= 6


def test_other_seeds_keep_invariants():
    for seed in (1, 2, 99):
        h, _ = _h(seed)
        gw = [i for i in h if i.severity == "SEV2" and i.cause_app == "api-gateway"
              and i.cause_change_kind == "deployment"]
        assert len(gw) == 10 and sum("Connection Timeout" in i.summary for i in h) == 3
