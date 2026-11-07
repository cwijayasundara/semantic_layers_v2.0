import polars as pl
from pp_helpers import derived, manifest, table

from e2ekg.preprocess.selector import Selector, select
from e2ekg.preprocess.traces_repair import edge_fingerprints, repair_traces


def raw_span_count(p) -> int:
    return manifest(p)["counts"]["spans"]


def test_orphans_marked_not_dropped(preprocessed_hero):
    t = table(preprocessed_hero, "spans")
    assert t["orphan"].sum() > 0 and t.height == raw_span_count(preprocessed_hero)
    o = t.filter(pl.col("orphan"))
    assert o["parent_span_id"].is_not_null().all()


def test_orphan_parent_really_missing(preprocessed_hero):
    t = table(preprocessed_hero, "spans")
    ids = t.select("trace_id", "span_id").unique()
    o = t.filter(pl.col("orphan")).join(
        ids, left_on=["trace_id", "parent_span_id"], right_on=["trace_id", "span_id"], how="semi")
    assert o.height == 0


def test_skew_not_applied_twice(preprocessed_hero):
    t = table(preprocessed_hero, "spans")
    assert (t["ts_utc_corrected"] == t["ts_utc"]).all()


def test_repair_leaves_already_corrected_time_alone():
    from datetime import UTC, datetime
    ts = datetime(2026, 10, 14, 20, tzinfo=UTC)
    df = pl.DataFrame({"trace_id": ["t", "t"], "span_id": ["a", "b"],
                       "parent_span_id": [None, "a"], "host": ["h1", "h2"],
                       "ts_utc": [ts, ts], "skew_ms": [0, 5000]})
    out = repair_traces(df, {"h2": 5000})
    assert out["ts_utc_corrected"].to_list() == [ts, ts]       # h2 not shifted a second time
    assert out["orphan"].to_list() == [False, False]


def test_zipkin_and_otlp_share_field_names(preprocessed_hero):
    t = table(preprocessed_hero, "spans")
    z = t.filter(pl.col("span_format") == "zipkin")
    assert z.height > 0
    for c in ("trace_id", "span_id", "name", "span_kind", "duration_us", "status_code"):
        assert z[c].null_count() == 0 or c in ("span_kind",)
    assert set(z["status_code"].unique()) <= {"ERROR", "UNSET", "OK"}


def test_edges_cover_every_span_and_are_sane(preprocessed_hero):
    e = derived(preprocessed_hero, "fp_edges")
    assert e["n"].sum() == table(preprocessed_hero, "spans").height
    assert (e["p50_ms"] <= e["p99_ms"]).all()
    assert e["error_rate"].is_between(0, 1).all()
    cross = e.filter(pl.col("caller_uri").is_not_null()
                     & (pl.col("caller_uri") != pl.col("callee_uri")))
    assert cross.height > 0


def test_pool_edge_is_visible(preprocessed_hero):
    """title-batch-loader -> title-metadata-svc is a real call edge in the traces."""
    e = derived(preprocessed_hero, "fp_edges")
    ab = e.filter(pl.col("caller_uri").str.ends_with("title-batch-loader")
                  & pl.col("callee_uri").str.ends_with("title-metadata-svc"))
    assert ab.height > 0


def test_edge_selector_round_trip(preprocessed_hero):
    e = derived(preprocessed_hero, "fp_edges")
    for row in e.sample(25, seed=5).iter_rows(named=True):
        got = select(preprocessed_hero.proc, Selector.from_json(row["selector"]))
        assert got.height == row["n"]


def test_edge_fingerprints_on_synthetic_trace():
    from datetime import UTC, datetime
    ts = datetime(2026, 10, 14, 20, 0, 5, tzinfo=UTC)
    df = pl.DataFrame({"raw_id": ["r1", "r2", "r3"], "span_index": [0, 0, 0],
                       "app_uri": ["A", "B", "B"], "trace_id": ["t"] * 3,
                       "span_id": ["1", "2", "3"], "parent_span_id": [None, "1", "2"],
                       "ts_utc": [ts] * 3, "duration_us": [1000, 4000, 2000],
                       "status_code": ["UNSET", "ERROR", "UNSET"]})
    e = edge_fingerprints(repair_traces(df)).sort("callee_uri", "caller_uri")
    ab = e.filter((pl.col("caller_uri") == "A") & (pl.col("callee_uri") == "B"))
    assert ab["n"][0] == 1 and ab["error_rate"][0] == 1.0 and ab["p50_ms"][0] == 4.0
    assert e["n"].sum() == 3
