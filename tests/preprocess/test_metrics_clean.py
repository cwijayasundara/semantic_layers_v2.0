import json
import re

import polars as pl
from pp_helpers import app_uri, derived, injections, table

from e2ekg.preprocess.metrics_clean import clean_metrics, metric_anomalies, metric_row_keys
from e2ekg.preprocess.selector import Selector, select


def clean(p):
    return derived(p, "metrics_clean")


def anomalies(p):
    return derived(p, "fp_metric_anomalies")


def test_seconds_normalized_original_kept(preprocessed_hero):
    m = clean(preprocessed_hero).filter(pl.col("unit_raw") == "s")
    assert m.height > 0
    assert (m["v"] == m["v_raw"] * 1000).all()
    ms = clean(preprocessed_hero).filter(
        pl.col("unit_raw").is_null() & ~pl.col("name").str.ends_with("_total"))
    assert (ms["v"].fill_null(0) == ms["v_raw"].fill_null(0)).all()


def test_every_injected_seconds_series_is_detected(preprocessed_hero):
    """met.unit_seconds is in the manifest (oracle, tests only): each (metric, app) it hit
    has unit_raw='s' points, and no other latency series does."""
    hit = set()
    for inj in injections(preprocessed_hero, "met.unit_seconds"):
        m = re.match(r"\w+:([\w.]+)(.*)", inj["detail"]["series"])
        app = re.search(r'"(?:nf\.app|app|service)", "([^"]+)"', m.group(2)).group(1)
        hit.add((m.group(1), app))
    c = clean(preprocessed_hero).filter(pl.col("unit_raw") == "s")
    got = {(n, a.removeprefix("https://kg.local/app/application/"))
           for n, a in zip(c["name"], c["app_uri"], strict=True) if a}   # drift names: no app
    from e2ekg.preprocess.identity import Resolver, catalog_from_table
    r = Resolver(catalog_from_table(table(preprocessed_hero, "catalog")), None)
    canon = {(n, r.resolve(a).app) for n, a in hit}
    canon = {x for x in canon if x[1]}                  # drift names resolve to no app
    assert got and len(got & canon) / len(canon) >= 0.9
    assert len(got - canon) <= 0.1 * len(got)


def test_app_tag_key_drift_is_one_series(preprocessed_hero):
    c = clean(preprocessed_hero)
    assert not c["series_key"].str.contains(r"nf\.app=|service=").any()
    assert c["tags_norm"].str.contains('"app"').all()


def test_counters_monotone_delta(preprocessed_hero):
    c = clean(preprocessed_hero).filter(pl.col("name").str.ends_with("_total"))
    assert c.height > 0 and (c["v"].drop_nulls() >= 0).all()
    assert c["v_raw"].is_not_null().all()


def test_dedupe_keeps_first_and_lists_others(preprocessed_hero):
    c = clean(preprocessed_hero)
    assert c.select("series_key", "t").is_duplicated().sum() == 0
    keys = derived(preprocessed_hero, "rows_metrics")
    points = keys.filter(~pl.col("key").str.ends_with("|meta")).height
    assert c.height + c["dup_raw_ids"].list.len().sum() == points
    d = c.filter(pl.col("dup_raw_ids").is_not_null()).row(0, named=True)
    assert d["raw_id"] not in d["dup_raw_ids"]


def test_gaps_are_flagged_not_dropped(preprocessed_hero):
    atlas = table(preprocessed_hero, "metrics_atlas")
    c = clean(preprocessed_hero)
    assert c["gap"].sum() > 0
    assert atlas["value"].null_count() > 0


def test_latency_anomaly_on_title_metadata_before_clients(preprocessed_hero):
    a = anomalies(preprocessed_hero)
    tms = a.filter((pl.col("app_uri") == app_uri("title-metadata-svc"))
                   & pl.col("name").str.starts_with("server.latency"))["t_start"].min()
    tv = a.filter((pl.col("app_uri") == app_uri("client-tv"))
                  & (pl.col("name") == "client.ttr.p90"))["t_start"].min()
    assert tms is not None and tv is not None and tms < tv


def test_anomalies_cover_answer_key_evidence(preprocessed_hero):
    ans = json.loads((preprocessed_hero.answers / "hero.json").read_text())
    ev = set(ans["evidence_by_tag"]["evidence:metric_anomaly"])
    got = set()
    for s in anomalies(preprocessed_hero)["selector"]:
        got |= set(select(preprocessed_hero.proc, Selector.from_json(s))["raw_id"])
    assert len(ev & got) / len(ev) >= 0.9


def test_every_metric_row_has_a_series(preprocessed_hero):
    rows = derived(preprocessed_hero, "rows_metrics")
    n = (table(preprocessed_hero, "metrics_atlas").height
         + table(preprocessed_hero, "metrics_prom").height)
    assert rows.height == n
    assert derived(preprocessed_hero, "fp_metric_series")["n"].sum() == n


def test_selector_round_trip(preprocessed_hero):
    for name in ("fp_metric_anomalies", "fp_metric_series"):
        df = derived(preprocessed_hero, name)
        for row in df.sample(min(20, df.height), seed=1).iter_rows(named=True):
            sel = Selector.from_json(row["selector"])
            got = select(preprocessed_hero.proc, sel)
            assert got.height > 0 and (got["key"] == sel.key).all()
            if name == "fp_metric_series":
                assert got.height == row["n"]


def _anomalies_of(vals, step_min=1, name="server.latency.p50"):
    from datetime import UTC, datetime, timedelta
    t0 = datetime(2026, 10, 14, 19, tzinfo=UTC)
    n = len(vals)
    ts = [t0 + timedelta(minutes=m * step_min) for m in range(n)]
    atlas = pl.DataFrame({"raw_id": [f"a{i}" for i in range(n)], "app_uri": ["x"] * n,
                          "ts_utc": pl.Series(ts).dt.replace_time_zone("UTC"),
                          "metric": [name] * n, "tags": ['{"nf.app":"x"}'] * n,
                          "value": vals})
    prom = pl.DataFrame(schema={"raw_id": pl.String, "prom_kind": pl.String,
                                "app_uri": pl.String, "ts_utc": atlas["ts_utc"].dtype,
                                "metric": pl.String, "labels": pl.String, "value": pl.Float64})
    return t0, atlas, prom, metric_anomalies(clean_metrics(atlas, prom))


def test_synthetic_change_point_and_reset():
    from datetime import timedelta
    vals = [10.0 + (m % 3) * 0.1 if m < 70 else 40.0 + (m % 3) * 0.1 for m in range(120)]
    t0, atlas, prom, a = _anomalies_of(vals)
    assert a.height == 1 and a["direction"][0] == "up"
    assert abs((a["t_start"][0] - (t0 + timedelta(minutes=70))).total_seconds()) <= 300
    assert metric_row_keys(atlas, prom).height == 120


# title-batch-loader grpc_client_pool_connections{state=active}, a 5-minute scrape series:
# noise 20..31, a two-point dip to 20 (inside that range), then the real step to 64.
POOL_ACTIVE = [31, 20, 23, 24, 30, 29, 30, 25, 22, 27, 28, 20, 31, 21, 22, 25, 29, 30, 25, 20,
               29, 24, 21, 24, 22, 23, 27, 31, 26, 22, 28, 28, 28, 30, 25, 20, 20, 29]


def test_two_point_dip_inside_noise_on_a_sparse_series_is_not_an_anomaly():
    """With a 5-minute scrape the 10-minute recent window held 3 points and the 30-minute
    baseline 6, so two low points flipped the median and 6 points gave a MAD far below the
    series' real spread: a dip inside the normal range scored 5.4 sigma."""
    *_, a = _anomalies_of([float(v) for v in POOL_ACTIVE], step_min=5,
                          name="grpc_client_pool_connections")
    assert a.height == 0


def test_real_step_on_a_sparse_series_is_still_detected_at_its_onset():
    from datetime import timedelta
    vals = [float(v) for v in POOL_ACTIVE] + [64.0] * 34
    t0, _, _, a = _anomalies_of(vals, step_min=5, name="grpc_client_pool_connections")
    assert a.height == 1 and a["direction"][0] == "up"
    assert abs((a["t_start"][0] - (t0 + timedelta(minutes=5 * 38))).total_seconds()) <= 600


def test_magnitude_stays_finite_when_the_baseline_is_zero():
    """grpc.client.pool.waiting is 0 until the incident: dividing by a ~0 spread gave 1.8e11."""
    import numpy as np
    rng = np.random.default_rng(3)
    vals = [0.0] * 40 + [float(v) for v in rng.integers(150, 210, 32)]
    *_, a = _anomalies_of(vals, step_min=5, name="grpc.client.pool.waiting")
    assert a.height == 1 and a["direction"][0] == "up"
    assert np.isfinite(a["magnitude"][0]) and a["magnitude"][0] < 1000


def _propagation_set(p) -> set[str]:
    """Apps that can see the fault: title-metadata-svc and everything upstream of it in the
    catalog's dependency edges."""
    callers: dict[str, set[str]] = {}
    for f in table(p, "catalog")["fields"]:
        d = json.loads(f)
        for dep in d.get("dependencies") or []:
            callers.setdefault(dep["app"], set()).add(d["id"])
    seen, todo = {"title-metadata-svc"}, ["title-metadata-svc"]
    while todo:
        for c in callers.get(todo.pop(), ()):
            if c not in seen:
                seen.add(c)
                todo.append(c)
    return seen


def _onset(p):
    ans = json.loads((p.answers / "hero.json").read_text())
    from datetime import datetime
    return datetime.fromisoformat(ans["answer_key"]["causal_chain"][0]["at_utc"])


def test_chain_latency_anomalies_point_up(preprocessed_hero):
    """Units are decided per stream: a point-wise rule converted the quiet period and left
    the larger post-incident values in seconds, so p50/p99 looked like they fell 1000x."""
    a = anomalies(preprocessed_hero)
    for app in ("title-metadata-svc", "title-batch-loader"):
        x = a.filter((pl.col("app_uri") == app_uri(app))
                     & pl.col("name").str.starts_with("server.latency"))
        assert x.height > 0 and set(x["direction"]) == {"up"}, app


def test_no_latency_stream_mixes_converted_and_raw_points(preprocessed_hero):
    c = clean(preprocessed_hero).filter(pl.col("name").str.contains("latency"))
    mixed = (c.group_by("series_key", "stream")
             .agg(pl.col("unit_raw").is_null().sum().alias("raw"),
                  pl.col("unit_raw").is_not_null().sum().alias("conv"))
             .filter((pl.col("raw") > 0) & (pl.col("conv") > 0)))
    assert mixed.height == 0


def test_anomaly_precision_nothing_before_onset_nothing_off_the_propagation_set(
        preprocessed_hero):
    """Counters included: their delta noise and diurnal trend once raised anomalies on
    api-gateway, evcache-titles, billing-* and cassandra-titles before the fault."""
    a = anomalies(preprocessed_hero)
    assert a.filter(pl.col("t_start") < _onset(preprocessed_hero)).height == 0
    names = {u.removeprefix("https://kg.local/app/application/")
             for u in a["app_uri"].drop_nulls()}
    assert names <= _propagation_set(preprocessed_hero), names - _propagation_set(
        preprocessed_hero)
    assert a["app_uri"].null_count() == 0


def test_unit_streams_match_the_injection_manifest(preprocessed_hero):
    """Every point of an injected (metric, app) stream is seconds, so a 1000x jump between
    neighbours of one series is a unit bug, not an incident."""
    c = clean(preprocessed_hero).filter(pl.col("name").str.contains("latency")).sort("t")
    jump = (c.with_columns(r=(pl.col("v") / pl.col("v").shift(1).over("series_key")))
            .filter((pl.col("r") > 300) | (pl.col("r") < 1 / 300)))
    assert jump.height == 0
