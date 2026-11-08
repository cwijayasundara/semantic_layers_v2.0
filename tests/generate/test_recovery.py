"""Recovery after the fix (spec section 6): the hero with its real fix added
through model_copy (the property reverted 12 -> 48 at 17:20 PT, the ASG scaling
out 12 -> 24 -> 36 -> 48 -> 56), at fixture scale."""
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from conftest import FIXTURE_SCALE

import e2ekg.generate.run as run
from e2ekg.generate.records import write_source
from e2ekg.generate.render_metrics import DIURNAL_AMP
from e2ekg.generate.run import baseline_scenario, generate_scenario, render_unmessed
from e2ekg.generate.world import World, as_of_utc, epoch, mitigation_attempts
from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.model import Change, Scenario
from e2ekg.timeutil import pt

TARGET = "title-metadata-svc"
ASG = "title-metadata-svc-v042"
NEW_APP = "lolomo-svc"


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
    return with_fix(plain_hero())


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


def plain_hero() -> Scenario:
    """The hero without any fix (its baseline, should hero.py carry one)."""
    return baseline_scenario(hero_scenario())


def with_new_series(s: Scenario) -> Scenario:
    """with_fix plus a post-fix scale-out of an app that has no ASG series, no
    scaling events and no asg metrics before: every key it makes is new."""
    assert not any(c.kind == "scaling" and c.app == NEW_APP for c in s.changes)
    s = with_fix(s)
    extra = Change(id="chg-asg-0201", kind="scaling", app=NEW_APP, at_utc=_pt(17, 22),
                   attrs={"asg": f"{NEW_APP}-v007", "previous_capacity": "6",
                          "desired_capacity": "9"}, is_root_cause=False)
    return s.model_copy(update={"changes": (*s.changes, extra)})


@dataclass
class Tree:
    root: Path
    recs: list      # the RawRecords generate_scenario wrote, in order
    ids: list[str]  # their raw ids, as write_source made them

    def manifest(self) -> dict:
        return json.loads((self.root.parent / "_answers" / "hero.manifest.json").read_text())


def _generate(s: Scenario, root: Path) -> Tree:
    seen = {}

    def spy(records, out):
        got = write_source(records, out)
        seen["recs"], seen["ids"] = list(records), [rid for rid, _ in got]
        return got

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(run, "write_source", spy)
        generate_scenario(s, root, scale=FIXTURE_SCALE)
    return Tree(root, seen["recs"], seen["ids"])


@pytest.fixture(scope="module")
def trees(tmp_path_factory, fixed) -> dict[str, Tree]:
    base = tmp_path_factory.mktemp("recovery")
    return {name: _generate(s, base / name / "hero") for name, s in
            (("plain", plain_hero()), ("fix", fixed), ("new", with_new_series(plain_hero())))}


def _late_relocated(t: Tree, cut) -> set[int]:
    """Indices of pre-cutoff records written after a later record of their file:
    met.late's late arrivals."""
    seen_post, out = set(), set()
    for k, r in enumerate(t.recs):
        if r.at_utc is not None and r.at_utc > cut:
            seen_post.add(r.rel_path)
        elif r.rel_path in seen_post:
            out.add(k)
    return out


def _bytes_key(rid: str, at) -> tuple:
    src, rest = rid.split("|", 1)
    rel, _off, length, digest = rest.rsplit("|", 3)
    return src, rel, length, digest, at


def assert_pre_cutoff_identical(plain: Tree, fx: Tree, cut) -> int:
    """Every record at or before the cutoff has the same raw id (source, file,
    offset, length, digest) and time in both trees, except met.late's late
    arrivals: those keep their bytes (length, digest) and time but sit after the
    post-cutoff data, so their offset (raw id) moves. Nothing else differs, and
    the fixed tree has no other record at or before the cutoff."""
    def pre(t: Tree):
        return [(k, rid, r.at_utc) for k, (r, rid) in enumerate(zip(t.recs, t.ids))
                if r.at_utc is None or r.at_utc <= cut]

    late = _late_relocated(plain, cut)
    p, f = pre(plain), pre(fx)
    f_ids = {rid: at for _, rid, at in f}
    moved = [(rid, at) for k, rid, at in p if k in late]
    kept = [(rid, at) for k, rid, at in p if k not in late]
    for rid, at in kept:
        assert rid in f_ids and f_ids[rid] == at, rid
    kept_ids = {rid for rid, _ in kept}
    rest = Counter(_bytes_key(rid, at) for rid, at in f_ids.items() if rid not in kept_ids)
    assert rest == Counter(_bytes_key(rid, at) for rid, at in moved)
    assert len(f) == len(p)
    # The exception set is exactly met.late's relocated points.
    assert all(rid.startswith("atlas|metrics/atlas.jsonl|") for rid, _ in moved)
    late_at = Counter(at for i in plain.manifest()["injections"]
                      if i["injector_id"] == "met.late"
                      for at in json.loads(i["detail"])["at_utc"])
    assert all(late_at[at.isoformat()] for _, at in moved)
    return len(moved)


def test_pre_cutoff_records_identical(trees, fixed):
    cut = as_of_utc(plain_hero())
    assert cut == as_of_utc(fixed)
    assert assert_pre_cutoff_identical(trees["plain"], trees["fix"], cut) > 0


def test_new_post_fix_series_leave_the_cutoff_alone(trees):
    """A fix that creates files, streams and series no earlier record has (an
    ASG series for an app that never scaled): still nothing before the cutoff
    changes, the stream-wide mess is the baseline's, and the new series is there."""
    cut = as_of_utc(plain_hero())
    assert_pre_cutoff_identical(trees["plain"], trees["new"], cut)
    assert _wide(trees["plain"]) == _wide(trees["new"])
    pts = [r for r in trees["new"].recs if r.source == "atlas"
           and r.fields["name"] == "asg.instances.inservice"
           and NEW_APP in json.dumps(r.fields["tags"]) and r.at_utc > cut]
    assert pts and max(r.fields["v"] for r in pts) == 9


def test_answers_record_the_fix(trees):
    plain, fx = trees["plain"].root, trees["fix"].root
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


WIDE = ("id.alias_surface", "id.casing", "id.suffix", "id.kind_confusion",
        "id.stale_catalog_alias", "met.tag_key_drift", "met.unit_seconds",
        "met.counter_reset", "met.duplicate_series", "log.tz_mix", "log.host_skew")


def _wide(t: Tree) -> set:
    out = set()
    for i in t.manifest()["injections"]:
        if i["injector_id"] in WIDE:
            d = json.loads(i["detail"])
            d.pop("count", None)
            out.add((i["injector_id"], i["rel_path"], json.dumps(d, sort_keys=True)))
    return out


def test_stream_wide_mess_is_the_same_after_the_cutoff(trees):
    """File-, stream- and series-wide corruptions (aliases, units, tag drift,
    counter resets, time zones, host skew) hit the post-cutoff records exactly as
    they hit the same files and series before it, so no stream changes its
    surface at the cutoff: the fixed run's manifest lists the same ones."""
    a = _wide(trees["plain"])
    assert a and a == _wide(trees["fix"])


def _undecodable(t: Tree) -> int:
    n = 0
    for r in t.recs:
        try:
            r.payload.decode("utf-8")
        except UnicodeDecodeError:
            n += 1
    return n


def test_manifest_lists_only_written_injections(trees):
    """A record-level injection after the cutoff is in the manifest only when
    its record is written: the mixed mess also corrupts the baseline's
    post-cutoff records, which the splice drops (log.encoding writes exactly
    one undecodable line per injection, so the two counts must agree)."""
    for name in ("plain", "fix"):
        t = trees[name]
        enc = [i for i in t.manifest()["injections"] if i["injector_id"] == "log.encoding"]
        assert enc and len(enc) == _undecodable(t), name
        dup = [i for i in t.manifest()["injections"] if i["injector_id"] == "log.duplicate"]
        logs = Counter((r.rel_path, r.payload) for r in t.recs if r.source == "logs"
                       or r.rel_path.startswith("logs/"))
        assert sum(n - 1 for n in logs.values()) >= len(dup) > 0, name
