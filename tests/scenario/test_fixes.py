"""Every fault scenario carries its real fix, after the agent's cut-off (spec section 6)."""
from datetime import timedelta

import pytest

from e2ekg.generate.run import baseline_scenario
from e2ekg.generate.world import as_of_utc, fix_change
from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.propagation import propagate
from e2ekg.scenario.variants import variant_scenarios
from e2ekg.timeutil import pt


def _all():
    return {s.name: s for s in (hero_scenario(), *variant_scenarios())}


def test_every_fault_variant_has_a_fix_after_cutoff():
    for name, s in _all().items():
        if name == "v-control":
            assert s.fault.fix_change_id is None and s.answer_key.fix_change_id is None
            assert s.answer_key.recovered_utc is None
            continue
        fix = fix_change(s)
        assert fix is not None, name
        assert s.answer_key.fix_change_id == s.fault.fix_change_id == fix.id, name
        assert not fix.is_root_cause, name
        assert as_of_utc(s) < fix.at_utc < s.window.end_utc - timedelta(minutes=30), name


def test_recovered_utc_is_the_last_degradation_end():
    for name, s in _all().items():
        if name == "v-control":
            continue
        degs = propagate(s.fault, s.edges, s.window, fixed_utc=fix_change(s).at_utc)
        assert s.answer_key.recovered_utc == max(d.end_utc for d in degs), name
        assert s.answer_key.recovered_utc < s.window.end_utc, name


def test_only_the_fix_and_its_recovery_follow_it():
    """baseline_scenario drops every change at or after the fix, so the scenario
    minus those is exactly the scenario as it was before fixes existed: nothing
    original lands at or after the fix time."""
    expected = {"hero": {"chg-cfg-0002", "chg-asg-0007", "chg-asg-0008", "chg-asg-0009",
                         "chg-asg-0010"}}
    for name, s in _all().items():
        if name == "v-control":
            continue
        dropped = {c.id for c in s.changes} - {c.id for c in baseline_scenario(s).changes}
        assert dropped == expected.get(name, {s.fault.fix_change_id}), name


def _pt(h, m):
    return pt(2026, 10, 14, h, m)


def test_hero_fix_reverts_the_property_and_scales_out():
    s = hero_scenario()
    by = {c.id: c for c in s.changes}
    fix = by["chg-cfg-0002"]
    assert (fix.kind, fix.app, fix.at_utc) == ("config_change", "title-metadata-svc", _pt(17, 20))
    assert fix.attrs == {"property": "title-metadata-svc.asg.minInstances", "old": "12",
                         "new": "48"}
    caps = [("12", "24"), ("24", "36"), ("36", "48"), ("48", "56")]
    for k, (prev, des) in enumerate(caps):
        c = by[f"chg-asg-{7 + k:04d}"]
        assert (c.kind, c.app, c.at_utc) == ("scaling", "title-metadata-svc", _pt(17, 21 + 2 * k))
        assert (c.attrs["previous_capacity"], c.attrs["desired_capacity"]) == (prev, des)
        assert c.attrs["asg"] == "title-metadata-svc-v042"


@pytest.mark.parametrize("name,kind,app,attrs", [
    ("v-bad-deploy", "deployment", "lolomo-svc",
     {"version": "lolomo-svc-v5.88.0", "strategy": "rollback"}),
    ("v-cache-storm", "config_change", "evcache-titles",
     {"property": "evcache-titles.ttl.seconds", "old": "60", "new": "86400"}),
    ("v-hot-partition", "config_change", "cassandra-titles",
     {"property": "cassandra-titles.titles.partition_key", "old": "region", "new": "titleId"}),
    ("v-cert-expiry", "config_change", "entitlement-svc",
     {"property": "entitlement-svc.tls.cert.serial", "old": "0xB77E", "new": "0xC9D2",
      "note": "re-issued cert"}),
    ("v-retry-storm", "deployment", "row-ranker",
     {"version": "row-ranker-v12.6.3", "strategy": "rollback"}),
])
def test_variant_fixes(name, kind, app, attrs):
    s = _all()[name]
    fix = fix_change(s)
    assert (fix.kind, fix.app, fix.attrs) == (kind, app, attrs)
    assert fix.at_utc == s.fault.start_utc + timedelta(minutes=150)
