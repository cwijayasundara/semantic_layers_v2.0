"""Processed tables as they were observable at a cutoff (R21, R34).

The live RCA runs as of a cutoff: the alert + 30 min. `scan(proc, name, as_of)`
is the one read path the harvest takes into a processed table. With
`as_of=None` it is the table itself; with a cutoff it is the table as it was
observable then, so a store harvested through it cannot hold a later fact.

  * A row is observable when it is stamped before the cutoff (`ts_utc < as_of`,
    so every half-open window ends at or before it). A row with no time cannot be
    shown to precede the cutoff and is left out. The catalog is reference data
    and is kept whole.
  * Rows a table derives from others follow them: a claim exists when its message
    does.
  * Fingerprints are recomputed over the observable rows with the Task 8 code
    that built them, so counts, magnitudes, percentiles and windows are what that
    code would have reported at the cutoff. A window still open at the cutoff ends
    there. Trace edges aggregate whole minutes, so spans of a minute the cutoff
    cuts into wait for the next one. Keys stay the full window's (template ids,
    series keys, edge keys), so every selector resolves against the full
    processed tables.

An unknown table name raises: a table without a rule here cannot reach the
as-of store by omission.

`live_cutoff(proc)` finds the cutoff from the processed tables alone (never
`_answers`, R5): the live incident is the one created last, its alert the one
behind the last page triggered at or before it opened, and the cutoff that
alert's first firing + 30 min."""
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

from e2ekg.preprocess import selector as sel
from e2ekg.preprocess.fingerprints import coverage_series
from e2ekg.preprocess.logtemplates import log_buckets, log_spikes
from e2ekg.preprocess.metrics_clean import clean_metrics, metric_anomalies
from e2ekg.preprocess.traces_repair import aggregate_edges

AS_OF_AFTER_ALERT = timedelta(minutes=30)     # R21


@dataclass(frozen=True)
class Cutoff:
    as_of: datetime
    incident_id: str
    alert_id: str | None      # None: the page named no alert with a firing record
    alert_at: datetime


def _path(proc: Path, name: str) -> Path:
    return Path(proc) / f"{name}.parquet"


def _timed(name: str) -> Callable[[Path, datetime], pl.LazyFrame]:
    return lambda proc, t: pl.scan_parquet(_path(proc, name)).filter(pl.col("ts_utc") < t)


def _whole(name: str) -> Callable[[Path, datetime], pl.LazyFrame]:
    return lambda proc, t: pl.scan_parquet(_path(proc, name))


def _claims(proc: Path, t: datetime) -> pl.LazyFrame:
    seen = _timed("derived/chat_messages")(proc, t).select("raw_id")
    return pl.scan_parquet(_path(proc, "derived/chat_claims")).join(seen, on="raw_id", how="semi")


def _logs(proc: Path, t: datetime) -> pl.DataFrame:
    return _timed("logs")(proc, t).select("app_uri", "template_id", "template", "ts_utc",
                                           "level").collect()


def _metric_inputs(proc: Path, t: datetime) -> tuple[pl.DataFrame, pl.DataFrame]:
    atlas = _timed("metrics_atlas")(proc, t).select(
        "raw_id", "app_uri", "ts_utc", "metric", "tags", "value").collect()
    prom = _timed("metrics_prom")(proc, t).select(
        "raw_id", "prom_kind", "app_uri", "ts_utc", "metric", "labels", "value").collect()
    return atlas, prom


def _ending_by(df: pl.DataFrame, t: datetime, source: str, key: str) -> pl.DataFrame:
    """A window still open at the cutoff ends there; its selector follows."""
    df = df.with_columns(pl.min_horizontal("t_end", pl.lit(t, pl.Datetime("us", "UTC")))
                         .alias("t_end"))
    sels = [sel.make(source, a, k, s, e)
            for a, k, s, e in df.select("app_uri", key, "t_start", "t_end").iter_rows()]
    return df.with_columns(selector=pl.Series(sels, dtype=pl.String))


def _edges(proc: Path, t: datetime) -> pl.DataFrame:
    whole_minutes = t.replace(second=0, microsecond=0)
    rows = _timed("derived/rows_spans")(proc, whole_minutes)
    spans = pl.scan_parquet(_path(proc, "spans")).select(
        "raw_id", "span_index", "duration_us", "status_code")
    keyed = rows.join(spans, on=["raw_id", "span_index"], how="left").with_columns(
        callee_uri=pl.col("app_uri"),
        caller_uri=pl.col("key").str.extract(r"^(.*)>", 1))
    keyed = keyed.with_columns(caller_uri=pl.when(pl.col("caller_uri") != "")
                               .then(pl.col("caller_uri")))
    return aggregate_edges(keyed.collect())


_DERIVED: dict[str, Callable[[Path, datetime], pl.DataFrame]] = {
    "derived/fp_log_buckets": lambda proc, t: log_buckets(_logs(proc, t)),
    "derived/fp_log_spikes": lambda proc, t: _ending_by(
        log_spikes(_logs(proc, t)), t, "logs", "template_id"),
    "derived/metrics_clean": lambda proc, t: clean_metrics(*_metric_inputs(proc, t)),
    "derived/fp_metric_series": lambda proc, t: coverage_series(
        _timed("derived/rows_metrics")(proc, t).collect(), "metrics"),
    "derived/fp_metric_anomalies": lambda proc, t: _ending_by(
        metric_anomalies(clean_metrics(*_metric_inputs(proc, t))), t, "metrics", "series_key"),
    "derived/fp_edges": _edges,
}

_ROWS: dict[str, Callable[[Path, datetime], pl.LazyFrame]] = {
    **{name: _timed(name) for name in (
        "alerts", "events", "incidents", "logs", "metrics_atlas", "metrics_prom", "pages",
        "slack", "spans", "derived/rows_logs", "derived/rows_metrics", "derived/rows_spans",
        "derived/chat_messages")},
    "catalog": _whole("catalog"),
    "derived/chat_claims": _claims,
}


def scan(proc: Path, name: str, as_of: datetime | None = None) -> pl.LazyFrame:
    """Table `name` (`<proc>/<name>.parquet`), as observable at `as_of` (None: all of it)."""
    if as_of is None:
        return pl.scan_parquet(_path(proc, name))
    if name in _DERIVED:
        return _DERIVED[name](Path(proc), as_of).lazy()
    if name not in _ROWS:
        raise KeyError(f"no as-of rule for table {name!r}")
    return _ROWS[name](Path(proc), as_of)


def live_cutoff(proc: Path) -> Cutoff:
    created = (scan(proc, "incidents").filter(
        (pl.col("event") == "incident.created") & pl.col("ts_utc").is_not_null())
        .sort("ts_utc", "raw_id").collect())
    if created.height == 0:
        raise ValueError(f"{proc}: no incident.created record to take the cutoff from")
    live = created.row(-1, named=True)
    pages = (scan(proc, "pages").filter(
        (pl.col("status") == "triggered") & (pl.col("ts_utc") <= live["ts_utc"]))
        .sort("ts_utc", "raw_id").collect())
    if pages.height == 0:
        raise ValueError(f"{proc}: no page before incident {live['incident_id']}")
    page = pages.row(-1, named=True)
    key = json.loads(page["fields"] or "{}").get("incident_key")
    firing = (scan(proc, "alerts").filter(
        (pl.col("alert_id") == key) & (pl.col("state") == "firing")
        & pl.col("ts_utc").is_not_null()).select(pl.col("ts_utc").min()).collect().item())
    alert_at = firing or page["ts_utc"]
    return Cutoff(as_of=alert_at + AS_OF_AFTER_ALERT, incident_id=live["incident_id"],
                  alert_id=key if firing else None, alert_at=alert_at)


__all__ = ["AS_OF_AFTER_ALERT", "Cutoff", "live_cutoff", "scan"]
