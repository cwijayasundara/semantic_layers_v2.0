import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
import yaml
from helpers import (
    hero_local,
    json_log_ts,
    jsonl,
    load_answers,
    manifest,
    slack_messages,
)

from e2ekg.generate import render_logs, render_metrics, render_traces
from e2ekg.generate.mess.traces import head_sampled
from e2ekg.generate.render_catalog import render_catalog
from e2ekg.generate.render_logs import LOG_FORMAT, zipf_probs
from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.history import TWIN_RESOLVER
from e2ekg.scenario.propagation import propagate
from e2ekg.timeutil import PT

HERO = "inc-5377-homepage-ttr"
LOG4J_HEAD = re.compile(rb"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} [A-Z]+ \[")
NGINX = re.compile(rb'^\S+ - \S+ \[[^\]]+\] "\S+ \S+ \S+" (\d{3}) ')


@pytest.fixture(scope="module")
def hero():
    return hero_scenario()


def test_realism_constants_on_every_renderer():
    for mod in (render_logs, render_metrics, render_traces):
        assert (mod.ZIPF_S, mod.DIURNAL_AMP, mod.TEMPLATES_PER_APP) == (1.3, 0.35, (20, 40))


def test_zipf_probs():
    p = zipf_probs(30)
    assert p.sum() == pytest.approx(1.0) and p[0] / p[1] == pytest.approx(2 ** 1.3)


# ---- catalog ----

def test_catalog_has_every_app_but_incomplete_aliases(hero, generated_hero):
    docs = list(yaml.safe_load_all((generated_hero / "catalog/apps.yaml").read_text()))
    assert {d["id"] for d in docs} == {a.id for a in hero.catalog}
    assert "TMS" not in (generated_hero / "catalog/apps.yaml").read_text()
    full = {a.id: set(a.aliases) for a in hero.catalog}
    assert any(set(d["aliases"]) < full[d["id"]] for d in docs)
    tbl = next(d for d in docs if d["id"] == "title-batch-loader")
    assert {"app": "title-metadata-svc", "protocol": "grpc", "pool_size": 64} in tbl["dependencies"]


def test_catalog_records_are_one_doc_each(hero):
    recs = render_catalog(hero, propagate(hero.fault, hero.edges, hero.window),
                          np.random.default_rng(1))
    assert len(recs) == len(hero.catalog)


# ---- metrics ----

def test_ttr_series_per_platform_and_cell(generated_hero):
    series = {(p["tags"].get("platform"), p["tags"].get("ab.cell"))
              for p in jsonl(generated_hero / "metrics/atlas.jsonl")
              if p.get("name") == "client.ttr.p90"}
    assert series == {(p, c) for p in ("tv", "android", "ios", "web") for c in "AB"}


def test_atlas_step_and_window(hero, generated_hero):
    ts = {p["t"] for p in jsonl(generated_hero / "metrics/atlas.jsonl")}
    lo, hi = (int(x.timestamp() * 1000) for x in (hero.window.start_utc, hero.window.end_utc))
    assert min(ts) >= lo and max(ts) < hi and all((t - lo) % 60_000 == 0 for t in ts)


def test_title_metadata_latency_rises_at_onset(generated_hero):
    pts = [p for p in jsonl(generated_hero / "metrics/atlas.jsonl")
           if p.get("name") == "server.latency.p99" and p.get("v") is not None
           and "title-metadata" in json.dumps(p["tags"]).lower()]
    onset = int(hero_local("15:05").timestamp() * 1000)
    before = [p["v"] for p in pts if p["t"] < onset]
    after = [p["v"] for p in pts if p["t"] >= onset + 300_000]
    # met.unit_seconds may have rescaled a whole series; compare medians per side.
    assert np.median(after) > 2 * np.median(before)


def test_prom_files_every_five_minutes_for_twenty_apps(generated_hero):
    files = sorted(p.name for p in (generated_hero / "metrics/prom").glob("*.prom"))
    apps = {f.rsplit("-", 1)[0] for f in files}
    assert len(apps) == 20 and len(files) == 20 * 72
    head = (generated_hero / "metrics/prom" / files[0]).read_text().splitlines()[0]
    assert head.startswith("# HELP ")


# ---- logs ----

def test_log_formats_follow_log_format(generated_hero):
    first = {app: next((generated_hero / "logs" / app).glob("*.log")).read_bytes()
             .split(b"\n", 1)[0] for app in LOG_FORMAT}
    assert LOG4J_HEAD.match(first["title-batch-loader"])
    assert NGINX.match(first["edge-gateway"])
    assert b"level=" in first["row-ranker"] and not first["row-ranker"].startswith(b"{")
    assert json.loads(first["homepage-graphql"])["msg"]


def test_pool_exhaustion_lines_after_onset_only(generated_hero):
    onset = hero_local("15:08")
    stamps = []
    for f in (generated_hero / "logs/title-batch-loader").glob("*.log"):
        for line in f.read_bytes().split(b"\n"):
            if b"Connection pool exhausted (active=64, idle=0, waiting=" in line:
                stamps.append(datetime.strptime(line[:23].decode(), "%Y-%m-%d %H:%M:%S,%f"))
    assert len(stamps) > 50
    # log.host_skew (+90 s) and log.tz_mix (naive PT) are the only offsets.
    pt_naive = onset.astimezone(PT).replace(tzinfo=None)
    utc_naive = onset.replace(tzinfo=None)
    first = min(stamps)
    assert first >= min(pt_naive, utc_naive) - timedelta(seconds=1)


def test_log_templates_are_zipf_heavy_tailed(generated_hero):
    msgs = Counter()
    for f in (generated_hero / "logs/homepage-graphql").glob("*.log"):
        for line in f.read_bytes().split(b"\n")[:20000]:
            try:
                msgs[re.sub(r"\d+|[0-9a-f]{8,}", "#", json.loads(line)["msg"])] += 1
            except (ValueError, KeyError):
                pass
    counts = [c for _, c in msgs.most_common()]
    assert len(counts) >= 15 and counts[0] > 3 * counts[len(counts) // 2]


def test_api_gateway_deploy_log_blip(generated_hero):
    def errors(lo, hi):
        n = 0
        for f in (generated_hero / "logs/api-gateway").glob("*.log"):
            for line in f.read_bytes().split(b"\n"):
                if b'"level":"ERROR"' in line:
                    ts = json_log_ts(line)
                    n += ts is not None and hero_local(lo) <= ts < hero_local(hi)
        return n
    assert errors("15:18", "15:22") > 5 * max(1, errors("14:00", "14:04"))


def test_edge_sheds_load_after_client_latency(generated_hero):
    first_503 = None
    for f in (generated_hero / "logs/edge-gateway").glob("*.log"):
        for line in f.read_bytes().split(b"\n"):
            m = NGINX.match(line)
            if m and m.group(1) == b"503":
                ts = datetime.strptime(line.split(b"[")[1].split(b"]")[0].decode(),
                                       "%d/%b/%Y:%H:%M:%S %z")
                first_503 = ts if first_503 is None else min(first_503, ts)
    assert first_503 is not None and first_503 >= hero_local("15:20")


def test_logs_stay_inside_window(hero, generated_hero):
    for f in (generated_hero / "logs/homepage-graphql").glob("*.log"):
        for line in f.read_bytes().split(b"\n")[::500]:
            ts = json_log_ts(line)
            if ts is not None:
                assert hero.window.start_utc <= ts < hero.window.end_utc


# ---- traces ----

def _trace_spans(root):
    spans = []
    for f in sorted((root / "traces").rglob("*.jsonl")):
        for rec in jsonl(f):
            if "resourceSpans" in rec or "scopeSpans" in rec:
                for s in rec["scopeSpans"][0]["spans"]:
                    spans.append((s["traceId"], s["status"].get("code") == 2))
            else:
                spans.append((rec["traceId"], "error" in rec.get("tags", {})))
    return spans


def test_only_head_sampled_or_error_traces_rendered(generated_hero):
    m = manifest(generated_hero)
    # trc.orphan re-exports a parent under a fresh id; both sides are mess.
    orphaned = {tid for i in m.injections if i["injector_id"] == "trc.orphan"
                for tid in (json.loads(i["detail"])["new_trace_id"],
                            json.loads(i["detail"])["trace_id"])}
    err: dict[str, bool] = {}
    for tid, e in _trace_spans(generated_hero):
        err[tid] = err.get(tid, False) or e
    assert len(err) > 100
    assert all(head_sampled(t, e) for t, e in err.items() if t not in orphaned)


def test_zipkin_legacy_files_exist(generated_hero):
    assert list((generated_hero / "traces/zipkin").glob("*.jsonl"))


def test_evidence_slow_traces_are_slow_lolomo_spans(generated_hero):
    ans = load_answers("hero", generated_hero.parent)
    ids = ans["evidence_by_tag"]["evidence:slow_trace"]
    assert ids
    onset = hero_local("15:13")
    slow = 0
    from e2ekg.rawid import RawSpan
    for rid in ids:
        s = RawSpan.parse(rid)
        with (generated_hero / s.rel_path).open("rb") as fh:
            fh.seek(s.offset)
            rec = json.loads(fh.read(s.length))
        if "scopeSpans" not in rec:
            continue
        attrs = {a["key"]: a["value"] for a in rec["resource"]["attributes"]}
        for sp in rec["scopeSpans"][0]["spans"]:
            start = datetime.fromtimestamp(int(sp["startTimeUnixNano"]) / 1e9, UTC)
            assert start >= onset
            dur_ms = (int(sp["endTimeUnixNano"]) - int(sp["startTimeUnixNano"])) / 1e6
            if "lolomo" in attrs["service.name"]["stringValue"].lower() and dur_ms > 1000:
                slow += 1
    assert slow >= 5


# ---- events ----

def test_incident_affected_service_corrected_after_40_min(generated_hero):
    recs = [r for r in jsonl(generated_hero / "incidents/incident_io.jsonl")
            if r.get("id") == "INC-5377"]
    assert [r["affected_services"] for r in recs][:2] == [["api-gateway"], ["homepage-graphql"]]
    t0, t1 = (datetime.fromisoformat(r["ts"].replace("Z", "+00:00")) for r in recs[:2])
    assert t1 - t0 == timedelta(minutes=40)


def test_change_events_carry_change_ids(hero, generated_hero):
    text = "".join((generated_hero / "events" / f).read_text()
                   for f in ("spinnaker.jsonl", "properties.log", "asg.jsonl",
                             "ab_allocations.csv"))
    for c in hero.changes:
        assert c.id in text, c.id
    props = (generated_hero / "events/properties.log").read_text()
    assert "title-metadata-svc.asg.minInstances" in props
    assert (generated_hero / "events/commits.log").read_text().startswith("commit ")


def test_history_incidents_and_pages_are_historical(hero, generated_hero):
    incs = [r for r in jsonl(generated_hero / "incidents/incident_io.jsonl")
            if r.get("id") != "INC-5377"]
    assert len({r["id"] for r in incs}) == len(hero.history)
    pages = jsonl(generated_hero / "pagerduty/pages.jsonl")
    old = [p for p in pages if datetime.fromisoformat(p["ts"].replace("Z", "+00:00"))
           < hero.window.start_utc]
    assert len(old) >= len(hero.history)


def test_hero_alert_fires_on_homepage(generated_hero):
    alerts = jsonl(generated_hero / "alerts/radar.jsonl")
    crit = [a for a in alerts if a.get("severity") == "critical" and a.get("state") == "firing"]
    assert any("homepage" in json.dumps(a).lower() for a in crit)


# ---- chat ----

def test_hero_channel_story(generated_hero):
    msgs = slack_messages(generated_hero, HERO)
    texts = [m["text"] for m in msgs]
    joined = "\n".join(texts)
    assert "chg-" not in joined and "root cause is" not in joined.lower()
    bump = next(i for i, t in enumerate(texts) if "bump min to 40" in t)
    cause = next(i for i, t in enumerate(texts) if "48" in t and "12" in t and "min" in t.lower())
    assert bump < cause
    twin = [m for m in msgs if m.get("user_profile", {}).get("name") == TWIN_RESOLVER]
    assert any("INC-" in m["text"] for m in twin)
    assert any("TMS" in t for t in texts) and any("the ranker" in t for t in texts)


def test_past_incident_channels(hero, generated_hero):
    chans = [p for p in (generated_hero / "slack").iterdir() if p.name != HERO]
    assert len(chans) == len(hero.history)
    for c in chans:
        msgs = slack_messages(generated_hero, c.name)
        assert 10 <= len(msgs) <= 40, c.name
        assert all(float(m["ts"]) < hero.window.start_utc.timestamp() for m in msgs)
