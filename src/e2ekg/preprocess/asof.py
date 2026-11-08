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
    cuts into wait for the next one. Series keys and edge keys stay the full
    window's: each is a function of its own row, so a later row cannot change it.
  * Log templates are not: Drain clusters a line with the others it is mined with,
    so lines after the cutoff would reshape the templates of lines before it (a
    rare line's template text and id). The as-of view therefore re-mines them with
    the preprocessing code (`mine_templates`) over the observable log lines alone,
    and everything keyed by a template follows: `logs` (template_id, template,
    params), `derived/rows_logs` (key), log buckets and spikes. Adding or changing
    any line after the cutoff cannot change them. The full tables keep the full
    window's templates. A log selector of an as-of store therefore names an as-of
    template id and resolves against `scan(proc, "derived/rows_logs", as_of)`
    (`selector.select(..., as_of=)`), not the full `rows_logs`.

An unknown table name raises: a table without a rule here cannot reach the
as-of store by omission.

`live_symptom(proc)` names what the live RCA investigates, from the processed
tables alone (never `_answers`, R5; R39): the latest incident created inside the
scenario window (the span the telemetry covers), or, when none opened there, the
window's highest-severity alert (ties: the earliest to fire). An incident created
before the window is history, never the symptom. `live_cutoff(proc)` is that
symptom's alert + 30 min: an incident's alert is the one named by its opening page
(`opening_page`: the last page triggered at or before it opened)."""
import functools
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import polars as pl

from e2ekg.preprocess import selector as sel
from e2ekg.preprocess.fingerprints import coverage_series
from e2ekg.preprocess.logtemplates import log_buckets, log_spikes, mine_templates
from e2ekg.preprocess.metrics_clean import clean_metrics, metric_anomalies
from e2ekg.preprocess.traces_repair import aggregate_edges

AS_OF_AFTER_ALERT = timedelta(minutes=30)     # R21
ALERT_SEVERITY = ("critical", "warning", "info")     # highest first


@dataclass(frozen=True)
class Symptom:
    kind: Literal["incident", "alert"]
    id: str                   # incident.io id or Radar alert id


@dataclass(frozen=True)
class Cutoff:
    as_of: datetime
    symptom: Symptom
    alert_id: str | None      # the symptom's alert; None: its page names none that fired
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


@functools.lru_cache(maxsize=4)
def _mined(path: Path, stamp: tuple[int, int], t: datetime) -> pl.DataFrame:
    """raw_id, template_id, template, params of the log lines before `t`, mined over those
    lines alone. `stamp` (mtime, size) keys the cache to the file's contents. Mining is
    independent of row order: Drain sees the distinct masked messages, sorted."""
    seen = pl.scan_parquet(path).filter(pl.col("ts_utc") < t).select("raw_id", "msg").collect()
    return mine_templates(seen).select("raw_id", "template_id", "template", "params")


def _observable_templates(proc: Path, t: datetime) -> pl.LazyFrame:
    path = _path(proc, "logs").resolve()
    st = path.stat()
    return _mined(path, (st.st_mtime_ns, st.st_size), t).lazy()


def _log_rows(proc: Path, t: datetime) -> pl.LazyFrame:
    """The observable log rows, their template columns re-mined as of `t`."""
    lf = _timed("logs")(proc, t)
    cols = lf.collect_schema().names()
    return (lf.drop("template_id", "template", "params")
            .join(_observable_templates(proc, t), on="raw_id", how="left", maintain_order="left")
            .select(cols))


def _log_keys(proc: Path, t: datetime) -> pl.LazyFrame:
    """`derived/rows_logs` as observable at `t`, keyed by the as-of template ids."""
    lf = _timed("derived/rows_logs")(proc, t)
    cols = lf.collect_schema().names()
    keys = _observable_templates(proc, t).select("raw_id", pl.col("template_id").alias("key"))
    return (lf.drop("key").join(keys, on="raw_id", how="left", maintain_order="left")
            .select(cols))


def _logs(proc: Path, t: datetime) -> pl.DataFrame:
    return _log_rows(proc, t).select("app_uri", "template_id", "template", "ts_utc",
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
        "alerts", "events", "incidents", "metrics_atlas", "metrics_prom", "pages",
        "slack", "spans", "derived/rows_metrics", "derived/rows_spans",
        "derived/chat_messages")},
    "logs": _log_rows,
    "derived/rows_logs": _log_keys,
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


def _window(proc: Path) -> tuple[datetime, datetime]:
    return scan(proc, "logs").select(pl.col("ts_utc").min().alias("lo"),
                                     pl.col("ts_utc").max().alias("hi")).collect().row(0)


def _first_firings(proc: Path) -> pl.DataFrame:
    """alert_id, severity, at: each alert's first firing record."""
    fired = (pl.col("state") == "firing") & pl.col("ts_utc").is_not_null()
    return (scan(proc, "alerts").filter(fired)
            .sort("ts_utc", "raw_id").group_by("alert_id", maintain_order=True)
            .agg(pl.col("severity").first(), pl.col("ts_utc").first().alias("at")).collect())


def opening_page(pages: list[dict], opened: datetime) -> dict | None:
    """The page that opened an incident: of the time-sorted triggered page records
    ({"ts_utc", ...}), the last at or before `opened`."""
    return next((p for p in reversed(pages) if p["ts_utc"] <= opened), None)


def live_symptom(proc: Path) -> Symptom | None:
    lo, hi = _window(proc)
    created = (scan(proc, "incidents").filter(
        (pl.col("event") == "incident.created") & pl.col("ts_utc").is_between(lo, hi))
        .sort("ts_utc", "raw_id").collect())
    if created.height:
        return Symptom("incident", created["incident_id"][-1])
    rank = {s: i for i, s in enumerate(ALERT_SEVERITY)}
    alerts = [r for r in _first_firings(proc).iter_rows(named=True) if lo <= r["at"] <= hi]
    if not alerts:
        return None
    top = min(alerts, key=lambda r: (rank.get(r["severity"], len(rank)), r["at"], r["alert_id"]))
    return Symptom("alert", top["alert_id"])


def live_cutoff(proc: Path) -> Cutoff:
    symptom = live_symptom(proc)
    if symptom is None:
        raise ValueError(f"{proc}: no incident or alert in the window to take the cutoff from")
    firing = dict(_first_firings(proc).select("alert_id", "at").iter_rows())
    if symptom.kind == "alert":
        return Cutoff(as_of=firing[symptom.id] + AS_OF_AFTER_ALERT, symptom=symptom,
                      alert_id=symptom.id, alert_at=firing[symptom.id])
    opened = (scan(proc, "incidents").filter(
        (pl.col("incident_id") == symptom.id) & (pl.col("event") == "incident.created"))
        .select(pl.col("ts_utc").min()).collect().item())
    pages = (scan(proc, "pages").filter(pl.col("status") == "triggered")
             .sort("ts_utc", "raw_id").collect().to_dicts())
    page = opening_page(pages, opened)
    if page is None:
        raise ValueError(f"{proc}: no page before incident {symptom.id}")
    key = json.loads(page["fields"] or "{}").get("incident_key")
    alert_at = firing.get(key) or page["ts_utc"]
    return Cutoff(as_of=alert_at + AS_OF_AFTER_ALERT, symptom=symptom,
                  alert_id=key if key in firing else None, alert_at=alert_at)


__all__ = ["AS_OF_AFTER_ALERT", "Cutoff", "Symptom", "live_cutoff", "live_symptom",
           "opening_page", "scan"]
