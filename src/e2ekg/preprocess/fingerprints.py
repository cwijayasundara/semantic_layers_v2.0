"""Layer 2 driver: fingerprints and the row-key tables behind every selector.

Reads the processed Parquet tables, and writes
  * logs.parquet gains template_id, template, params; spans.parquet gains orphan and
    ts_utc_corrected (an alias of ts_utc, see traces_repair);
  * derived/rows_{logs,metrics,spans}.parquet: one row per parsed row with the key a
    selector matches on (see `selector`);
  * derived/fp_*.parquet: the fingerprint tables. Their selectors, together,
    reach every parsed row of logs, metrics and spans:
      fp_log_buckets (every log row), fp_log_spikes,
      fp_metric_series (every metric row), fp_metric_anomalies,
      fp_edges (every span)."""
from pathlib import Path

import polars as pl

from e2ekg.preprocess import selector as sel
from e2ekg.preprocess.logtemplates import log_buckets, log_spikes, mine_templates
from e2ekg.preprocess.metrics_clean import clean_metrics, metric_anomalies, metric_row_keys
from e2ekg.preprocess.traces_repair import edge_fingerprints, edge_keys, repair_traces

FP_TABLES = ("fp_log_buckets", "fp_log_spikes", "fp_metric_series", "fp_metric_anomalies",
             "fp_edges")


def _replace_parquet(path: Path, lf: pl.LazyFrame) -> None:
    tmp = path.with_suffix(".tmp")
    lf.sink_parquet(tmp, compression="zstd")
    tmp.replace(path)


def coverage_series(rows: pl.DataFrame, source: str) -> pl.DataFrame:
    """One selector per (app, key) spanning its rows; rows with no time get their own."""
    timed = (rows.filter(pl.col("ts_utc").is_not_null()).group_by("app_uri", "key")
             .agg(pl.col("ts_utc").min().alias("t_start"),
                  (pl.col("ts_utc").max() + pl.duration(microseconds=1)).alias("t_end"),
                  pl.len().alias("n")))
    untimed = (rows.filter(pl.col("ts_utc").is_null()).group_by("app_uri", "key")
               .agg(pl.len().alias("n"))
               .with_columns(t_start=pl.lit(None, pl.Datetime("us", "UTC")),
                             t_end=pl.lit(None, pl.Datetime("us", "UTC"))))
    g = pl.concat([timed, untimed.select(timed.columns)]).sort("app_uri", "key", "t_start",
                                                                 nulls_last=True)
    sels = [sel.make(source, a, k, s, e) for a, k, s, e in
            g.select("app_uri", "key", "t_start", "t_end").iter_rows()]
    return g.with_columns(selector=pl.Series(sels, dtype=pl.String))


def build_fingerprints(proc: Path, span_skew: dict[str, int] | None = None) -> dict[str, int]:
    proc = Path(proc)
    d = proc / "derived"
    d.mkdir(exist_ok=True)
    for stale in d.glob("*.parquet"):
        stale.unlink()
    counts: dict[str, int] = {}

    # logs
    logs = pl.read_parquet(proc / "logs.parquet",
                           columns=["raw_id", "app_uri", "ts_utc", "level", "msg"])
    logs = mine_templates(logs)
    logs.select("raw_id", "app_uri", "ts_utc", pl.col("template_id").alias("key")).write_parquet(
        d / "rows_logs.parquet", compression="zstd")
    log_buckets(logs).write_parquet(d / "fp_log_buckets.parquet", compression="zstd")
    log_spikes(logs).write_parquet(d / "fp_log_spikes.parquet", compression="zstd")
    tcols = logs.select("raw_id", "template_id", "template", "params").lazy()
    _replace_parquet(proc / "logs.parquet",
                     pl.scan_parquet(proc / "logs.parquet").join(tcols, on="raw_id", how="left"))
    del logs, tcols

    # metrics
    atlas = pl.read_parquet(proc / "metrics_atlas.parquet",
                            columns=["raw_id", "app_uri", "ts_utc", "metric", "tags", "value"])
    prom = pl.read_parquet(proc / "metrics_prom.parquet",
                           columns=["raw_id", "prom_kind", "app_uri", "ts_utc", "metric",
                                    "labels", "value"])
    mrows = metric_row_keys(atlas, prom)
    mrows.write_parquet(d / "rows_metrics.parquet", compression="zstd")
    coverage_series(mrows, "metrics").write_parquet(d / "fp_metric_series.parquet",
                                                    compression="zstd")
    clean = clean_metrics(atlas, prom)
    clean.write_parquet(d / "metrics_clean.parquet", compression="zstd")
    metric_anomalies(clean).write_parquet(d / "fp_metric_anomalies.parquet", compression="zstd")
    del atlas, prom, mrows, clean

    # spans
    spans = pl.read_parquet(proc / "spans.parquet", columns=[
        "raw_id", "span_index", "app_uri", "trace_id", "span_id", "parent_span_id", "ts_utc",
        "duration_us", "status_code"])
    spans = repair_traces(spans, span_skew)
    ek = edge_keys(spans)
    ek.select("raw_id", "span_index", "app_uri", "ts_utc", "key").write_parquet(
        d / "rows_spans.parquet", compression="zstd")
    edge_fingerprints(spans).write_parquet(d / "fp_edges.parquet", compression="zstd")
    scols = spans.select("raw_id", "span_index", "orphan", "ts_utc_corrected").lazy()
    _replace_parquet(proc / "spans.parquet",
                     pl.scan_parquet(proc / "spans.parquet").join(
                         scols, on=["raw_id", "span_index"], how="left"))
    for t in FP_TABLES:
        counts[t] = pl.scan_parquet(d / f"{t}.parquet").select(pl.len()).collect().item()
    return counts


def load_fingerprints(proc: Path) -> dict[str, pl.DataFrame]:
    d = Path(proc) / "derived"
    return {t: pl.read_parquet(d / f"{t}.parquet") for t in FP_TABLES}


__all__ = ["FP_TABLES", "build_fingerprints", "coverage_series", "load_fingerprints"]
