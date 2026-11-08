"""The after-cut-off ending: the full store and the processed tables after `as_of`, labelled
as such. The hero fixture is generated with its fix (17:20 PT revert, scale-outs after)."""
import functools
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from e2ekg.explain.after import LABEL, after_cutoff
from e2ekg.explain.case import build_case


FIX = datetime(2026, 10, 15, 0, 20, tzinfo=UTC)     # chg-cfg-0002, 17:20 PT


@functools.cache
def _world():
    from e2ekg.generate.world import World
    from e2ekg.scenario.hero import hero_scenario
    return World(hero_scenario(), ())


def _diurnal(t: str) -> float:
    from e2ekg.generate.render_metrics import DIURNAL_AMP
    from e2ekg.generate.world import epoch
    return float(_world().diurnal(epoch(_when(t)), DIURNAL_AMP))


def _when(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _chain(run):
    return run["report"]["hypotheses"][0]["chain"]


@pytest.fixture(scope="module")
def case(live_tools, engine_run):
    return build_case(live_tools.store, live_tools.tools, "hero", engine_run)


@pytest.fixture(scope="module")
def after(case, engine_run, harvested_hero, preprocessed_hero):
    return after_cutoff(harvested_hero.store, preprocessed_hero.proc, case["meta"],
                        _chain(engine_run))


def test_label_and_as_of(after, case):
    assert after["label"] == LABEL == "after the agent's data cut-off"
    assert after["as_of"] == case["meta"]["as_of"]


def test_after_cutoff_shows_fix_and_recovery(after, case):
    cfg = [c for c in after["changes"] if c["kind"] == "config_change"]
    assert any(c["what"].endswith("->48") for c in cfg), cfg
    alert = case["meta"]["alert"]
    [s] = [s for s in after["series"] if s["name"] == alert["metric"] and s["app"] == alert["app"]]
    # The day curve alone lifts TTR ~25% from the first point (onset - 30 min, ~14:30 PT) to
    # the last (17:59 PT), fault or not; divided out, the symptom is back where it started
    # and was well above it before the fix (tests/generate/test_recovery.py does the same).
    level = [(t, v / _diurnal(t)) for t, v in s["points"]]
    first, last = level[0][1], level[-1][1]
    assert abs(last / first - 1) < 0.10, (first, last)
    assert max(v for t, v in level if _when(t) < FIX) > 1.3 * first


def test_series_cover_the_chain_anomalies(after, case, engine_run):
    names = {(s["name"], s["app"]) for s in after["series"]}
    assert len(names) == len(after["series"])          # one series per (metric, app)
    assert len(names) >= 2                               # the alert's and at least one anomaly's
    onset = _when(case["meta"]["onset_at"] or case["meta"]["alert"]["at"])
    for s in after["series"]:
        ts = [_when(t) for t, _ in s["points"]]
        assert ts == sorted(ts) and len(set(ts)) == len(ts)
        assert (onset - ts[0]).total_seconds() <= 30 * 60 + 60
        assert all(t.second == 0 for t in ts)


def test_after_only_returns_post_cutoff_changes(after, case, live_tools):
    as_of = _when(case["meta"]["as_of"])
    assert after["changes"]
    assert all(_when(c["at"]) > as_of for c in after["changes"])
    seen = {c["uri"] for c in json.loads(live_tools.tools["changes_in_window"].invoke(
        {"apps": [], "start": "2026-10-14T00:00Z", "end": case["meta"]["as_of"]}))}
    assert not seen & {c["uri"] for c in after["changes"]}
    # the failed bumps (before the fix) and the scale-outs (after it) are both here
    caps = [c["what"] for c in after["changes"] if c["kind"] == "scaling"]
    assert any("->40" in w for w in caps) and any("->56" in w for w in caps), caps


@pytest.fixture
def client(tmp_path, monkeypatch, harvested_hero, harvested_hero_as_of, preprocessed_hero,
           engine_run):
    from e2ekg.api import app as app_module
    g = tmp_path / "graph/hero"
    g.mkdir(parents=True)
    (g / "store").symlink_to(harvested_hero.out / "store")
    (g / "harvest.json").symlink_to(harvested_hero.out / "harvest.json")
    (g / "as_of").symlink_to(harvested_hero_as_of.out)
    (tmp_path / "processed").mkdir()
    (tmp_path / "processed/hero").symlink_to(preprocessed_hero.proc)
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw/hero").symlink_to(preprocessed_hero.raw)
    run = tmp_path / "evals/r1"
    (run / "runs").mkdir(parents=True)
    (run / "runs/hero__graph__0.json").write_text(json.dumps(engine_run))
    monkeypatch.setattr(app_module, "DATA", tmp_path)
    for f in (app_module._store, app_module._tools_cached):
        f.cache_clear()
    yield TestClient(app_module.app)
    for f in (app_module._store, app_module._tools_cached):
        f.cache_clear()


def test_after_endpoint(client, after):
    r = client.get("/api/scenarios/hero/after")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["label"] == LABEL
    assert body["changes"] == after["changes"] and body["series"] == after["series"]
    r = client.get("/api/scenarios/hero/after", params={"source": "eval:nope"})
    assert r.status_code == 404


def test_changes_are_not_capped(harvested_hero, preprocessed_hero, case):
    """Over the whole window the agent's changes_in_window is cut to its character cap
    (the last row carries the note); the after view keeps every change."""
    from e2ekg.agent.tools import make_tools
    from e2ekg.explain.after import _changes, _closure
    alert = case["meta"]["alert"]
    lo, hi = _when("2026-10-14T19:00:00Z"), _when("2026-10-15T01:00:00Z")
    apps = _closure(harvested_hero.store, alert["app"])
    tools = make_tools(harvested_hero.store, preprocessed_hero.proc, "hero", "after-test",
                       as_of=_when(case["meta"]["as_of"]))
    closure = json.loads(tools["dependency_closure"].invoke({"app": alert["app"],
                                                             "direction": "down"}))
    assert {r["id"] for r in closure if "id" in r} == set(apps)
    capped = json.loads(tools["changes_in_window"].invoke(
        {"apps": apps, "start": "2026-10-14T19:00:01Z", "end": hi.isoformat()}))
    assert {"truncated"} & set(capped[-1])            # the agent's view is cut here
    got = _changes(harvested_hero.store, apps, lo, hi)
    assert len(got) > len(capped)
    assert [c["uri"] for c in got[:len(capped)]] == [c["uri"] for c in capped]
    assert {(c["uri"], c["what"]) for c in got} >= {(c["uri"], c["what"]) for c in capped}


def test_aggregate_series_says_so(after, case):
    alert = case["meta"]["alert"]
    s = after["series"][0]
    assert (s["name"], s["app"]) == (alert["metric"], alert["app"])
    assert s["aggregate"] == "mean"
    assert s["apps"] == ["client-android", "client-ios", "client-tv", "client-web"]
    assert all(x["aggregate"] is None and x["apps"] == [x["app"]] for x in after["series"][1:])
