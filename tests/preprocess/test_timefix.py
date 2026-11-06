"""Time normalization, per-file zone inference and clock-skew recovery."""
from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest
from pp_helpers import injected_skew_host, injections, skew_table, table

from e2ekg.preprocess.timefix import estimate_skew, infer_tz, normalize_ts

T = datetime(2026, 10, 14, 21, 52, 3, 250000, tzinfo=UTC)  # 14:52:03.250 PDT


@pytest.mark.parametrize(("raw", "hint"), [
    ("2026-10-14T21:52:03.250Z", None),
    ("2026-10-14T14:52:03.250-07:00", None),
    ("2026-10-14T14:52:03.250", "America/Los_Angeles"),
    ("1792014723250", None),                      # epoch ms
    ("1792014723250000", None),                   # epoch us (zipkin)
    ("1792014723250000000", None),                # epoch ns (otlp)
    ("1792014723.250000", None),                  # slack ts
    ("2026-10-14 21:52:03,250", None),            # log4j, naive -> UTC by default
    ("2026-10-14 21:52:03,250", "UTC"),
    ("2026-10-14 14:52:03,250", "America/Los_Angeles"),
])
def test_normalize_ts(raw, hint):
    assert normalize_ts(raw, hint) == T


@pytest.mark.parametrize("raw", ["14/Oct/2026:14:52:03 -0700", "14/Oct/2026:21:52:03 +0000",
                                 "Wed Oct 14 14:52:03 2026 -0700"])
def test_normalize_ts_second_resolution(raw):
    assert normalize_ts(raw, None) == T.replace(microsecond=0)


def test_normalize_ts_is_dst_aware():
    winter = normalize_ts("2026-12-01 09:00:00,000", "America/Los_Angeles")
    assert winter == datetime(2026, 12, 1, 17, 0, tzinfo=UTC)


def test_normalize_ts_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_ts("half past noon", None)


def test_infer_tz_picks_the_zone_that_lines_up():
    ref = int(T.timestamp() * 1e6)
    assert infer_tz("2026-10-14 14:52:00,000", ref) == "America/Los_Angeles"
    assert infer_tz("2026-10-14 21:50:00,000", ref) == "UTC"


def _pairs(skews: dict[str, int], n: int = 400, seed: int = 3) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    hosts = sorted(skews)
    rows = []
    t0 = int(T.timestamp() * 1e6)
    for i in range(n):
        a, b = rng.choice(len(hosts), 2, replace=False)
        ca, cb = hosts[a], hosts[b]
        t = t0 + int(rng.integers(0, 3_600_000_000))
        lat = int(2000 + 6000 * rng.random())
        rid = f"{i:016x}"
        rows.append((ca, rid, "Calling x.GetY", t + skews[ca] * 1000))
        rows.append((cb, rid, f"Received GetY from {ca}", t + lat + skews[cb] * 1000))
    df = pl.DataFrame(rows, schema=["host", "request_id", "msg", "ts_us"], orient="row")
    return df.with_columns(pl.col("ts_us").cast(pl.Datetime("us", "UTC")).alias("ts_utc"))


def _no_spans() -> pl.DataFrame:
    return pl.DataFrame(schema={"trace_id": pl.String, "span_id": pl.String,
                                "parent_span_id": pl.String, "host": pl.String,
                                "ts_utc": pl.Datetime("us", "UTC")})


def test_estimate_skew_recovers_one_skewed_host():
    skews = {f"h{i}": 0 for i in range(12)} | {"h3": 90_000}
    got = estimate_skew(_no_spans(), _pairs(skews))
    assert got["h3"] == pytest.approx(90_000, abs=50)
    assert all(v == 0 for h, v in got.items() if h != "h3")


def test_estimate_skew_negative_and_unpaired_hosts():
    skews = {f"h{i}": 0 for i in range(8)} | {"h1": -30_000}
    logs = pl.concat([_pairs(skews), pl.DataFrame(
        {"host": ["lonely"], "request_id": [None], "msg": ["hello"],
         "ts_us": [0]}).with_columns(pl.col("ts_us").cast(pl.Datetime("us", "UTC"))
                                     .alias("ts_utc"))], how="vertical_relaxed")
    got = estimate_skew(_no_spans(), logs)
    assert got["h1"] == pytest.approx(-30_000, abs=50) and got["lonely"] == 0


def test_estimate_skew_falls_back_to_span_clock_offsets():
    t0 = T
    rows = []
    for i in range(50):
        tid = f"{i:032x}"
        start = t0 + timedelta(seconds=i)
        rows.append((tid, f"p{i}", None, "front", start))
        rows.append((tid, f"c{i}", f"p{i}", "back", start + timedelta(milliseconds=5, seconds=20)))
        for k in range(4):
            rows.append((tid, f"d{i}-{k}", f"p{i}", f"other{k}",
                         start + timedelta(milliseconds=3 + k)))
    spans = pl.DataFrame(rows, schema=["trace_id", "span_id", "parent_span_id", "host", "ts_utc"],
                         orient="row").with_columns(pl.col("ts_utc").dt.convert_time_zone("UTC"))
    logs = pl.DataFrame({"host": ["back", "front"], "request_id": [None, None],
                         "msg": ["a", "b"], "ts_utc": [T, T]})
    got = estimate_skew(spans, logs)
    assert got["back"] == pytest.approx(20_000, abs=50) and got["front"] == 0


# ---- full pipeline on the scale-0.05 hero fixture ----

def test_skew_recovered(preprocessed_hero):
    skew = skew_table(preprocessed_hero)
    skewed = injected_skew_host(preprocessed_hero)
    assert skew[skewed] == pytest.approx(90_000, abs=5_000)
    assert {h for h, v in skew.items() if v} == {skewed}


def test_skew_applied_to_the_skewed_host_only(preprocessed_hero):
    df = table(preprocessed_hero, "logs")
    skewed = injected_skew_host(preprocessed_hero)
    by_host = df.group_by(pl.col("host") == skewed).agg(pl.col("skew_ms").unique())
    got = {k: sorted(v) for k, v in by_host.iter_rows()}
    assert got[False] == [0] and len(got[True]) == 1 and got[True][0] != 0


def test_every_log_lands_in_the_hero_window(preprocessed_hero):
    """Naive PT log4j files (log.tz_mix) must not land 7 h early."""
    df = table(preprocessed_hero, "logs")
    lo = datetime(2026, 10, 14, 19, 0, tzinfo=UTC) - timedelta(minutes=5)
    hi = datetime(2026, 10, 15, 1, 0, tzinfo=UTC) + timedelta(minutes=5)
    assert df.filter((pl.col("ts_utc") < lo) | (pl.col("ts_utc") > hi)).height == 0


def test_tz_mix_files_get_a_zone_hint(preprocessed_hero):
    df = table(preprocessed_hero, "logs")
    naive_pt = {i["rel_path"] for i in injections(preprocessed_hero, "log.tz_mix")
                if i["detail"]["style"] == "log4j_pt"}
    assert naive_pt
    hints = dict(df.filter(pl.col("tz_hint").is_not_null())
                 .group_by("rel_path").agg(pl.col("tz_hint").first()).iter_rows())
    assert {p for p, z in hints.items() if z == "America/Los_Angeles"} == naive_pt
