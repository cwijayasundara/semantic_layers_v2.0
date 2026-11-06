"""Metric cleaning and change-point anomalies.

`clean_metrics` unifies Atlas and Prometheus points into one long frame:
  * app tag keys `nf.app|app|service` become `app`, valued with the resolved
    catalog id when the row resolved (else the written value). A series that
    changed key mid-stream (met.tag_key_drift) is therefore one series.
  * series_key = `<name>|<k=v,...>` over the sorted normalized tags.
  * units: a series in seconds is scaled to ms (`v = v_raw * 1000`, `unit_raw="s"`).
    Seconds are recognised by a metric name ending `_seconds`, a `unit=s` tag/label,
    or, as the Atlas/Prometheus exports carry no unit marker, by a latency point
    whose value is under 1/50 of the median of its peers (same name and non-app
    tags, at least 50 points). The decision is per point, because the generator's
    unit injection can leave half a series in ms; originals stay in `v_raw`.
  * counters (`*_total`): `v` is the per-interval delta; a negative delta is a
    counter reset and the delta is the new value (counting restarted from zero),
    so `v` is never negative. The first point of a counter has no delta (null).
  * gaps: a null value stays as a row with `gap=True`.
  * duplicates: `(series_key, t)` keeps the first row; the other raw ids go to
    `dup_raw_ids`. Rows are never dropped from the parsed tables, only collapsed here.

`metric_anomalies` finds change points: at each point, the median of the last 10
minutes against the median of the 30 minutes before that, scaled by the baseline's
MAD (1.4826*MAD, floored at 5% of the baseline level so a flat series does not
alarm on noise). |shift| >= 5 flags the point; consecutive flags make one anomaly that
lasts until the smoothed level returns within 5 sigma of the pre-change baseline,
whose start is backdated by the detector's lag (the recent median needs a majority of
its window past the change)."""
import json
import warnings
from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
from numpy.lib.stride_tricks import sliding_window_view

from e2ekg.preprocess import selector as sel

APP_KEYS = ("nf.app", "app", "service")
RECENT_MIN, BASE_MIN, SHIFT_MIN, FLOOR_FRAC = 10, 30, 5.0, 0.05
# A change must also be big in absolute terms: counters' per-interval deltas follow the
# diurnal curve and are noisy (reset streams, multi-interval gaps), so they need a 50%
# level change; other series 10%. Sigma statistics alone flag a smooth trend.
REL_MIN, COUNTER_REL_MIN, COUNTER_FLOOR_FRAC = 0.10, 0.50, 0.25
SECONDS_RATIO, MIN_PEERS = 1 / 50, 50
APP_URI_PREFIX = "https://kg.local/app/application/"


def _norm_tags(tags_json: str | None, app_uri: str | None) -> dict[str, str]:
    d = json.loads(tags_json) if tags_json else {}
    out: dict[str, str] = {}
    app = None
    for k, v in d.items():
        if k in APP_KEYS:
            app = app or v
        else:
            out[k] = str(v)
    if app_uri:
        app = app_uri.removeprefix(APP_URI_PREFIX)
    if app is not None:
        out["app"] = str(app)
    return dict(sorted(out.items()))


def series_key(name: str, tags: dict[str, str]) -> str:
    return f"{name}|" + ",".join(f"{k}={v}" for k, v in tags.items())


def _points(atlas_df: pl.DataFrame, prom_df: pl.DataFrame) -> pl.DataFrame:
    a = atlas_df.select("raw_id", "app_uri", "ts_utc", pl.col("metric").alias("name"),
                        pl.col("tags").alias("_tags"), pl.col("value").alias("v_raw"))
    p = (prom_df.filter(pl.col("prom_kind") == "sample")
         .select("raw_id", "app_uri", "ts_utc", pl.col("metric").alias("name"),
                 pl.col("labels").alias("_tags"), pl.col("value").alias("v_raw")))
    return pl.concat([a, p], how="vertical_relaxed")


def _keyed(pts: pl.DataFrame) -> pl.DataFrame:
    """Adds tags_norm, series_key, _unit_tag, _sub (raw stream id) and _peer to point rows."""
    norm = [_norm_tags(t, u) for t, u in zip(pts["_tags"], pts["app_uri"], strict=True)]
    names = pts["name"].to_list()
    pts = pts.with_columns(
        tags_norm=pl.Series([json.dumps(n, separators=(",", ":")) for n in norm]),
        series_key=pl.Series([series_key(nm, n) for nm, n in zip(names, norm, strict=True)]),
        _unit_tag=pl.Series([n.get("unit") for n in norm], dtype=pl.String),
        _sub=pl.Series([json.dumps(sorted(json.loads(x).items()) if x else [])
                        for x in pts["_tags"]]),
        _peer=pl.Series([series_key(nm, {k: v for k, v in n.items()
                                          if k not in ("app", "instance")})
                         for nm, n in zip(names, norm, strict=True)])).drop("_tags")
    return pts


def metric_row_keys(atlas_df: pl.DataFrame, prom_df: pl.DataFrame) -> pl.DataFrame:
    """raw_id, app_uri, ts_utc, key for EVERY metric row: points get their series_key
    (before deduplication), Prometheus HELP/TYPE lines get `<name>|meta`."""
    k = _keyed(_points(atlas_df, prom_df)).select(
        "raw_id", "app_uri", "ts_utc", pl.col("series_key").alias("key"))
    meta = prom_df.filter(pl.col("prom_kind") != "sample").select(
        "raw_id", "app_uri", "ts_utc", (pl.col("metric").fill_null("") + "|meta").alias("key"))
    return pl.concat([k, meta], how="vertical_relaxed")


def clean_metrics(atlas_df: pl.DataFrame, prom_df: pl.DataFrame) -> pl.DataFrame:
    pts = _keyed(_points(atlas_df, prom_df))
    pts = pts.with_columns(
        gap=pl.col("v_raw").is_null() | pl.col("v_raw").is_nan(), t=pl.col("ts_utc"))

    # dedupe by (series_key, t), keeping the first raw_id in input order
    dups = (pts.group_by("series_key", "t", maintain_order=True)
            .agg(pl.col("raw_id").slice(1).alias("dup_raw_ids"))
            .with_columns(dup_raw_ids=pl.when(pl.col("dup_raw_ids").list.len() > 0)
                          .then(pl.col("dup_raw_ids"))))
    pts = (pts.unique(subset=["series_key", "t"], keep="first", maintain_order=True)
           .join(dups, on=["series_key", "t"], how="left"))

    # seconds -> ms, decided per emitter stream (the raw tag set as written), never per
    # point: a stream is one unit end to end, and a point-wise rule would convert the
    # quiet period and leave the post-incident (larger) values unconverted, inverting
    # the anomaly. A stream is seconds when its own LOW level (10th percentile, so an
    # incident that lifts the stream cannot hide it) is under 1/50 of the median of its
    # peers (same name and non-app tags, at least 50 points). Normal latency spread across
    # apps is about 40x and ms-vs-s is 1000x, so 1/50 separates them. A short stream
    # (under 5 points; a one-off surface form) can sit wholly inside an incident, where
    # the peer rule is blind, so it is re-decided against its own series: seconds when
    # x1000 lands nearer the series' neighbouring values (within 15 min) than x1.
    pos = ~pl.col("gap") & (pl.col("v_raw") > 0)
    peer = (pts.filter(pos).group_by("_peer")
            .agg(pl.col("v_raw").median().alias("_peer_med"), pl.len().alias("_peer_n")))
    stream = (pts.filter(pos).group_by("_peer", "_sub")
              .agg(pl.col("v_raw").quantile(0.1, "lower").alias("_stream_med"),
                   pl.len().alias("_stream_n")))
    pts = pts.join(peer, on="_peer", how="left").join(stream, on=["_peer", "_sub"], how="left")
    pts = pts.with_columns(_inferred=pl.col("name").str.contains("latency")
                           & (pl.col("_peer_n") >= MIN_PEERS)
                           & (pl.col("_stream_med") < pl.col("_peer_med") * SECONDS_RATIO))
    is_sec = (pl.col("name").str.ends_with("_seconds") | (pl.col("_unit_tag") == "s")
              | pl.col("_inferred").fill_null(False))
    pts = pts.with_columns(_sec=is_sec.fill_null(False)).with_row_index("_i")
    v1 = pl.when(pl.col("_sec")).then(pl.col("v_raw") * 1000).otherwise(pl.col("v_raw"))
    ref = (pts.filter(pl.col("name").str.contains("latency") & (pl.col("_stream_n") >= 5)
                      & pos).select("series_key", "t", v1.alias("_ref")).sort("t"))
    tiny = (pts.filter(pl.col("name").str.contains("latency") & (pl.col("_stream_n") < 5)
                       & pos & ~pl.col("_sec")).select("_i", "series_key", "t", "v_raw").sort("t"))
    if tiny.height and ref.height:
        tol = timedelta(minutes=15)
        before = tiny.join_asof(ref.rename({"_ref": "_b"}), on="t", by="series_key",
                                strategy="backward", tolerance=tol, check_sortedness=False)
        both = before.join_asof(ref.rename({"_ref": "_f"}), on="t", by="series_key",
                                strategy="forward", tolerance=tol, check_sortedness=False)

        def nearer(ref_col: str) -> pl.Expr:     # True/False/null (no neighbour on that side)
            return ((pl.col("v_raw") * 1000 / pl.col(ref_col)).log().abs()
                    < (pl.col("v_raw") / pl.col(ref_col)).log().abs())
        # flip only when every neighbour agrees: next to an incident edge one side is 100x away
        near = (both.with_columns(_flip=pl.when(pl.col("_b").is_null() & pl.col("_f").is_null())
                                  .then(False)
                                  .otherwise(nearer("_b").fill_null(True)
                                             & nearer("_f").fill_null(True)))
                .filter(pl.col("_flip")).select("_i", "_flip"))
        pts = pts.join(near, on="_i", how="left", maintain_order="left")
        pts = pts.with_columns(_sec=pl.col("_sec") | pl.col("_flip").fill_null(False))
    pts = pts.with_columns(unit_raw=pl.when(pl.col("_sec")).then(pl.lit("s")), v=pl.col("v_raw"))
    pts = pts.with_columns(v=pl.when(pl.col("unit_raw") == "s").then(pl.col("v_raw") * 1000)
                           .otherwise(pl.col("v")))

    # counters -> monotone per-interval deltas. A counter's history is only meaningful
    # within one emitter stream (the raw tag set as written: surface form, tag key), so
    # deltas are taken inside (series_key, stream) and scaled to the series' usual step.
    counter = pl.col("name").str.ends_with("_total")
    pts = pts.sort("series_key", "_sub", "t", maintain_order=True)
    g = ["series_key", "_sub"]
    prev = pl.col("v_raw").shift(1).over(g)
    dt = (pl.col("t") - pl.col("t").shift(1).over(g)).dt.total_microseconds()
    step = dt.median().over("series_key")
    delta = pl.when(pl.col("v_raw") < prev).then(pl.col("v_raw")).otherwise(
        pl.col("v_raw") - prev)
    delta = delta * (step / dt).fill_null(1.0)
    pts = pts.with_columns(v=pl.when(counter).then(delta).otherwise(pl.col("v")))
    pts = pts.sort("series_key", "t", maintain_order=True)
    return pts.select("series_key", "app_uri", "name", "tags_norm", "t", "v", "v_raw",
                      "unit_raw", "gap", "raw_id", "dup_raw_ids", pl.col("_sub").alias("stream"))


def _runs(flag: np.ndarray):
    i, n = 0, len(flag)
    while i < n:
        if not flag[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and flag[j + 1]:
            j += 1
        yield i, j
        i = j + 1


def metric_anomalies(clean_df: pl.DataFrame) -> pl.DataFrame:
    schema = {"series_key": pl.String, "app_uri": pl.String, "name": pl.String,
              "t_start": pl.Datetime("us", "UTC"), "t_end": pl.Datetime("us", "UTC"),
              "direction": pl.String, "magnitude": pl.Float64, "selector": pl.String}
    rows = []
    df = clean_df.filter(pl.col("t").is_not_null()).sort("series_key", "t")
    for (key,), g in df.group_by(["series_key"], maintain_order=True):
        t = g["t"].to_numpy().astype("datetime64[us]").astype("int64")
        if len(t) < 8:
            continue
        step = int(np.median(np.diff(t))) or 60_000_000
        t0 = t[0]
        idx = ((t - t0) // step).astype(int)
        n = int(idx.max()) + 1
        x = np.full(n, np.nan)
        x[idx] = g["v"].to_numpy()
        r_n = max(3, round(RECENT_MIN * 60_000_000 / step))
        b_n = max(6, round(BASE_MIN * 60_000_000 / step))
        pad = np.full(r_n + b_n, np.nan)
        xp = np.concatenate([pad, x])
        recent = sliding_window_view(xp, r_n)[b_n + 1: b_n + 1 + n]       # x[i-r_n+1 .. i]
        base = sliding_window_view(xp, b_n)[1: 1 + n]                      # x[i-r_n-b_n+1 .. i-r_n]
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            rec_med = np.nanmedian(recent, axis=1)
            b_med = np.nanmedian(base, axis=1)
            mad = np.nanmedian(np.abs(base - b_med[:, None]), axis=1)
        valid = ((~np.isnan(base)).sum(axis=1) >= max(6, b_n // 2)) & (
            (~np.isnan(recent)).sum(axis=1) >= 2)
        counter = g["name"][0].endswith("_total")
        floor = COUNTER_FLOOR_FRAC if counter else FLOOR_FRAC
        sigma = np.maximum(1.4826 * mad, floor * np.abs(b_med))
        sigma = np.where(sigma > 0, sigma, 1e-9)
        shift = (rec_med - b_med) / sigma
        rel = np.abs(rec_med - b_med) / np.maximum(np.abs(b_med), 1e-12)
        flag = valid & (np.abs(shift) >= SHIFT_MIN) & (
            rel >= (COUNTER_REL_MIN if counter else REL_MIN))
        covered = -1
        for i, j in _runs(flag):
            if i <= covered:
                continue
            # the episode lasts while the smoothed level stays shifted from the
            # pre-change baseline (a plateau is one anomaly, not a few minutes of one)
            k = j
            b0, s0 = b_med[i], sigma[i]
            while k + 1 < n and abs(rec_med[k + 1] - b0) / s0 >= SHIFT_MIN and (
                    abs(rec_med[k + 1] - b0) / max(abs(b0), 1e-12)
                    >= (COUNTER_REL_MIN if counter else REL_MIN)):
                k += 1
            covered = k
            j = k
            lead = r_n - (r_n // 2 + 1)    # the median flips once most of the window has moved
            ts = _ts(t0 + max(i - lead, 0) * step)
            te = _ts(t0 + (j + 1) * step)
            seg = shift[i:j + 1]
            seg = seg[~np.isnan(seg)]
            peak = float(seg[np.argmax(np.abs(seg))])
            app = g["app_uri"][0]
            rows.append({"series_key": key, "app_uri": app, "name": g["name"][0],
                         "t_start": ts, "t_end": te,
                         "direction": "up" if peak > 0 else "down",
                         "magnitude": abs(peak),
                         "selector": sel.make("metrics", app, key, ts, te)})
    return pl.DataFrame(rows, schema=schema).sort("t_start", "series_key")


def _ts(us: int):
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=int(us))
