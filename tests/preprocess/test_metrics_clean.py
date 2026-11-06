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


def test_synthetic_change_point_and_reset():
    from datetime import UTC, datetime, timedelta
    t0 = datetime(2026, 10, 14, 19, tzinfo=UTC)
    ts = [t0 + timedelta(minutes=m) for m in range(120)]
    vals = [10.0 + (m % 3) * 0.1 if m < 70 else 40.0 + (m % 3) * 0.1 for m in range(120)]
    atlas = pl.DataFrame({"raw_id": [f"a{i}" for i in range(120)], "app_uri": ["x"] * 120,
                          "ts_utc": pl.Series(ts).dt.replace_time_zone("UTC"),
                          "metric": ["server.latency.p50"] * 120,
                          "tags": ['{"nf.app":"x"}'] * 120, "value": vals})
    prom = pl.DataFrame(schema={"raw_id": pl.String, "prom_kind": pl.String,
                                "app_uri": pl.String, "ts_utc": atlas["ts_utc"].dtype,
                                "metric": pl.String, "labels": pl.String, "value": pl.Float64})
    a = metric_anomalies(clean_metrics(atlas, prom))
    assert a.height == 1 and a["direction"][0] == "up"
    assert abs((a["t_start"][0] - (t0 + timedelta(minutes=70))).total_seconds()) <= 300
    assert metric_row_keys(atlas, prom).height == 120
