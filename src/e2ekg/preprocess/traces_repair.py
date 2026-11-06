"""Trace repair and edge fingerprints.

`repair_traces` adds `orphan` (the span names a parent that is not in the data,
whether lost to sampling or a broken export) and `ts_utc_corrected`.

Time: Task 6's `ts_utc` ALREADY has the host skew removed (`ts_utc = parsed -
skew_ms`). `ts_utc_corrected` is therefore an alias of `ts_utc` and `skew` is not
applied again; the argument stays in the signature as the host->ms table that was
applied upstream. Zipkin spans arrive in the OTLP field names already (the parser
maps traceId/id/parentId/kind/timestamp/duration and tags); this function does not
touch them, and orphans are marked, never dropped.

`edge_fingerprints` groups every span by (caller app, callee app, minute): the
callee is the span's own app, the caller the app of its parent span (empty for
roots and orphans, the same app for in-process child spans). Every span row falls
in exactly one group, so the edge table covers the whole spans table."""
from datetime import timedelta

import polars as pl

from e2ekg.preprocess import selector as sel

UNIT_US = 1000.0


def repair_traces(spans_df: pl.DataFrame, skew: dict[str, int] | None = None) -> pl.DataFrame:
    ids = spans_df.select("trace_id", "span_id").unique()
    parent = ids.rename({"span_id": "_pid"}).with_columns(_found=pl.lit(True))
    out = (spans_df.join(parent, left_on=["trace_id", "parent_span_id"],
                         right_on=["trace_id", "_pid"], how="left", maintain_order="left")
           .with_columns(orphan=pl.col("parent_span_id").is_not_null()
                         & pl.col("_found").is_null(),
                         ts_utc_corrected=pl.col("ts_utc"))
           .drop("_found"))
    return out


def edge_keys(spans_df: pl.DataFrame) -> pl.DataFrame:
    """Per span row: raw_id, span_index, app_uri (callee), caller_uri, edge key."""
    first = (spans_df.select("trace_id", "span_id", pl.col("app_uri").alias("caller_uri"))
             .unique(subset=["trace_id", "span_id"], keep="first", maintain_order=True))
    j = spans_df.join(first, left_on=["trace_id", "parent_span_id"],
                      right_on=["trace_id", "span_id"], how="left", maintain_order="left")
    return j.with_columns(
        callee_uri=pl.col("app_uri"),
        key=pl.col("caller_uri").fill_null("") + pl.lit(">") + pl.col("app_uri").fill_null(""))


def edge_fingerprints(spans_df: pl.DataFrame) -> pl.DataFrame:
    return aggregate_edges(edge_keys(spans_df))


def aggregate_edges(keyed: pl.DataFrame) -> pl.DataFrame:
    """Keyed spans (caller_uri, callee_uri, key, ts_utc, duration_us, status_code) -> one
    row per (edge, minute)."""
    e = keyed.with_columns(
        minute=pl.col("ts_utc").dt.truncate("1m"),
        _ms=pl.col("duration_us") / UNIT_US,
        _err=(pl.col("status_code") == "ERROR").cast(pl.Float64))
    g = (e.group_by("caller_uri", "callee_uri", "minute", "key")
         .agg(pl.col("_ms").quantile(0.5, "linear").alias("p50_ms"),
              pl.col("_ms").quantile(0.99, "linear").alias("p99_ms"),
              pl.col("_err").mean().alias("error_rate"), pl.len().alias("n"))
         .sort("callee_uri", "caller_uri", "minute", nulls_last=True))
    sels = [sel.make("spans", callee, key, m, None if m is None else m + timedelta(minutes=1))
            for callee, key, m in g.select("callee_uri", "key", "minute").iter_rows()]
    return g.with_columns(selector=pl.Series(sels, dtype=pl.String)).select(
        "caller_uri", "callee_uri", "minute", "p50_ms", "p99_ms", "error_rate", "n",
        "selector", "key")
