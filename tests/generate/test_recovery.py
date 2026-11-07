"""Recovery after the fix (spec section 6): the hero with its real fix added
through model_copy (the property reverted 12 -> 48 at 17:20 PT, the ASG scaling
out 12 -> 24 -> 36 -> 48 -> 56), at fixture scale."""
import json
from collections import defaultdict

import numpy as np
import pytest
from conftest import FIXTURE_SCALE

from e2ekg.generate.mess import apply_mess
from e2ekg.generate.render_metrics import DIURNAL_AMP
from e2ekg.generate.run import generate_scenario, render_unmessed
from e2ekg.generate.world import World, as_of_utc, epoch, mitigation_attempts
from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.model import Change, Scenario
from e2ekg.timeutil import pt

TARGET = "title-metadata-svc"
ASG = "title-metadata-svc-v042"


def _pt(h: int, m: int):
    return pt(2026, 10, 14, h, m)


def with_fix(s: Scenario) -> Scenario:
    fix = Change(id="chg-cfg-0101", kind="config_change", app=TARGET, at_utc=_pt(17, 20),
                 attrs={"property": f"{TARGET}.asg.minInstances", "old": "12", "new": "48"},
                 is_root_cause=False)
    caps = (12, 24, 36, 48, 56)
    outs = tuple(Change(id=f"chg-asg-{101 + k:04d}", kind="scaling", app=TARGET,
                        at_utc=_pt(17, 21 + 2 * k),
                        attrs={"asg": ASG, "previous_capacity": str(caps[k]),
                               "desired_capacity": str(caps[k + 1])}, is_root_cause=False)
                 for k in range(4))
    return s.model_copy(update={
        "fault": s.fault.model_copy(update={"fix_change_id": fix.id}),
        "changes": (*s.changes, fix, *outs),
        "answer_key": s.answer_key.model_copy(update={"fix_change_id": fix.id})})


@pytest.fixture(scope="module")
def fixed() -> Scenario:
    return with_fix(hero_scenario())


@pytest.fixture(scope="module")
def fixed_metrics(fixed):
    return render_unmessed(fixed, FIXTURE_SCALE, families=("metrics",)).records["metrics"]


def _atlas(recs, name):
    return [r for r in recs if r.source == "atlas" and r.fields["name"] == name]


def test_capacity_recovers(fixed_metrics):
    pts = [r for r in _atlas(fixed_metrics, "asg.instances.inservice")
           if r.fields["tags"]["nf.app"] == TARGET]
    assert pts
    after = [r.fields["v"] for r in pts if r.at_utc >= _pt(17, 35)]
    assert after and min(after) >= 48
    before = [r.fields["v"] for r in pts if _pt(16, 0) <= r.at_utc < _pt(17, 20)]
    assert max(before) == 12


def test_ttr_recovers(fixed, fixed_metrics):
    """client.ttr.p90, with the evening diurnal curve divided out (17:55 sits ~40%
    higher on the day curve than 13:30, fault or not), is back within 10% of its
    13:00-14:00 level by 17:50-18:00, and was well above it before the fix."""
    w = World(fixed, ())
    pts = _atlas(fixed_metrics, "client.ttr.p90")

    def level(lo, hi):
        vs = [r.fields["v"] / float(w.diurnal(epoch(r.at_utc), DIURNAL_AMP)) for r in pts
              if lo <= r.at_utc < hi]
        assert vs
        return float(np.mean(vs))

    base = level(_pt(13, 0), _pt(14, 0))
    assert level(_pt(16, 30), _pt(17, 20)) > 1.3 * base
    assert abs(level(_pt(17, 50), _pt(18, 0)) / base - 1) < 0.10


def test_failed_bumps_still_reset_to_floor(fixed):
    ms = mitigation_attempts(fixed)
    assert len(ms) == 2
    assert all(m.floor == 12 and m.bumped_to == 40 for m in ms)
    assert all(m.at_utc < _pt(17, 20) for m in ms)


@pytest.fixture(scope="module")
def generated_pair(tmp_path_factory, fixed):
    base = tmp_path_factory.mktemp("recovery")
    plain, fx = base / "plain" / "hero", base / "fix" / "hero"
    generate_scenario(hero_scenario(), plain, scale=FIXTURE_SCALE)
    generate_scenario(fixed, fx, scale=FIXTURE_SCALE)
    return plain, fx


def _join(rs) -> bytes:
    return b"".join(x.payload + b"\n" for x in rs)


def test_pre_cutoff_records_identical(generated_pair, fixed):
    """Every raw record at or before as_of (ground-truth time, from the generator's
    own messy record list of the unfixed hero) is byte-identical with and without
    the fix. Records in time order before the cutoff keep their byte offsets (so
    their raw ids); the few atlas points `met.late` appends after everything else
    in the file stay byte-identical and stay at the end of the file."""
    plain, fx = generated_pair
    hero = hero_scenario()
    cut = as_of_utc(hero)
    assert cut == as_of_utc(fixed)
    r = render_unmessed(hero, FIXTURE_SCALE)
    recs, _ = apply_mess(r.all_records(), r.mess_rng, hero)
    per_file = defaultdict(list)
    for rec in recs:
        per_file[rec.rel_path].append(rec)
    checked = 0
    for rel, rs in per_file.items():
        assert (plain / rel).read_bytes() == _join(rs)  # the in-memory list is what was written
        pre = [x.at_utc is None or x.at_utc <= cut for x in rs]
        k = pre.index(False) if False in pre else len(rs)
        late = [x for x, p in zip(rs[k:], pre[k:]) if p]
        b = (fx / rel).read_bytes()
        assert b.startswith(_join(rs[:k])), rel   # same bytes at the same offsets
        assert b.endswith(_join(late)), rel
        checked += k + len(late)
    assert checked > 0.5 * len(recs)
    # Nothing new before the cutoff anywhere: a file only the fixed run has holds no
    # record at or before the cutoff (its lines all come from after it).
    assert {p.relative_to(plain) for p in plain.rglob("*") if p.is_file()} <= \
        {p.relative_to(fx) for p in fx.rglob("*") if p.is_file()}


def test_answers_record_the_fix(generated_pair):
    plain, fx = generated_pair
    a = json.loads((fx.parent / "_answers" / "hero.json").read_text())
    assert a["fix_change_id"] == "chg-cfg-0101"
    assert a["recovered_utc"] is not None and a["recovered_utc"] > "2026-10-15T00:20:00Z"
    p = json.loads((plain.parent / "_answers" / "hero.json").read_text())
    assert p["fix_change_id"] is None and p["recovered_utc"] is None


def test_post_fix_data_shows_recovery(fixed):
    """The fix and the scale-outs are change events, and the planted
    pool-exhaustion lines stop once title-batch-loader has recovered."""
    r = render_unmessed(fixed, FIXTURE_SCALE, families=("events", "logs")).records
    props = [x.fields for x in r["events"] if x.rel_path == "events/properties.log"]
    assert any(f["old"] == "12" and f["new"] == "48" and f["ts"] == _pt(17, 20) for f in props)
    asg = [x.fields for x in r["events"] if x.rel_path == "events/asg.jsonl"]
    assert [f["desired_capacity"] for f in asg if f["ts"] > _pt(17, 20)] == [24, 36, 48, 56]
    pool = [x.at_utc for x in r["logs"] if x.rel_path.startswith("logs/title-batch-loader/")
            and "Connection pool exhausted" in str(x.fields.get("msg"))]
    assert any(_pt(16, 30) <= t < _pt(17, 0) for t in pool)
    assert not any(t >= _pt(17, 50) for t in pool)


def test_stream_wide_mess_is_the_same_after_the_cutoff(generated_pair):
    """File-, stream- and series-wide corruptions (aliases, units, tag drift,
    counter resets, time zones, host skew) hit the post-cutoff records exactly as
    they hit the same files and series before it, so no stream changes its
    surface at the cutoff: the fixed run's manifest lists the same ones."""
    plain, fx = generated_pair
    wide = ("id.alias_surface", "id.casing", "id.suffix", "id.kind_confusion",
            "id.stale_catalog_alias", "met.tag_key_drift", "met.unit_seconds",
            "met.counter_reset", "met.duplicate_series", "log.tz_mix", "log.host_skew")

    def injections(root):
        m = json.loads((root.parent / "_answers" / "hero.manifest.json").read_text())
        out = set()
        for i in m["injections"]:
            if i["injector_id"] in wide:
                d = json.loads(i["detail"])
                d.pop("count", None)
                out.add((i["injector_id"], i["rel_path"], json.dumps(d, sort_keys=True)))
        return out

    a, b = injections(plain), injections(fx)
    assert a and a == b
