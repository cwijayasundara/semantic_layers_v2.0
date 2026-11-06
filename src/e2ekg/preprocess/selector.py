"""Raw selectors: a fingerprint's pointer back to the parsed rows it summarizes.

A selector names a row source (`logs`, `metrics` or `spans`), an application, a
source-specific key and a half-open time window `[t_start, t_end)` on `ts_utc`:
  * logs: key = template_id
  * metrics: key = series_key (help/type lines use `<name>|meta`)
  * spans: key = `<caller_uri>><callee_uri>` (caller empty for roots and orphans);
    app_uri is the span's own (callee) app
`app_uri=None` selects rows with no resolved app; `key=None` means any key.
`t_start == t_end` is the sentinel for rows with no parseable time.

`select` resolves a selector with DuckDB over `<proc>/derived/rows_<source>.parquet`,
which holds one row per parsed row (spans: per span) with its key. A fingerprint's
count therefore equals `select(...).height` by construction."""
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import polars as pl

SOURCES = ("logs", "metrics", "spans")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Selector:
    source: str
    app_uri: str | None
    key: str | None
    t_start: datetime
    t_end: datetime

    def to_json(self) -> str:
        d = asdict(self)
        d["t_start"], d["t_end"] = self.t_start.isoformat(), self.t_end.isoformat()
        return json.dumps(d, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, s: str) -> "Selector":
        d = json.loads(s)
        return cls(d["source"], d["app_uri"], d["key"], datetime.fromisoformat(d["t_start"]),
                   datetime.fromisoformat(d["t_end"]))


def window(t_start: datetime | None, t_end: datetime | None) -> tuple[datetime, datetime]:
    """Sentinel window for rows with no time."""
    return (EPOCH, EPOCH) if t_start is None else (t_start, t_end)


def make(source: str, app_uri: str | None, key: str | None, t_start: datetime | None,
         t_end: datetime | None) -> str:
    s, e = window(t_start, t_end)
    return Selector(source, app_uri, key, s, e).to_json()


def rows_path(proc_root: Path, source: str) -> Path:
    return Path(proc_root) / "derived" / f"rows_{source}.parquet"


def select(proc_root: Path, sel: Selector) -> pl.DataFrame:
    if sel.source not in SOURCES:
        raise ValueError(f"unknown selector source {sel.source!r}")
    where, params = ["app_uri IS NOT DISTINCT FROM ?"], [sel.app_uri]
    if sel.key is not None:
        where.append("key = ?")
        params.append(sel.key)
    if sel.t_start == sel.t_end:
        where.append("ts_utc IS NULL")
    else:
        where.append("ts_utc >= ? AND ts_utc < ?")
        params += [sel.t_start, sel.t_end]
    con = duckdb.connect()
    try:
        # path is interpolated; parameters carry every user-controlled value
        q = (f"SELECT * FROM read_parquet('{rows_path(proc_root, sel.source).as_posix()}') "
             f"WHERE {' AND '.join(where)}")
        return pl.from_arrow(con.sql(q, params=params).to_arrow_table())
    finally:
        con.close()


def minute_end(t: datetime) -> datetime:
    return t + timedelta(minutes=1)


def selected_raw_ids(proc_root: Path, selectors: list[str]) -> set[str]:
    """Raw ids of every row any of `selectors` (JSON) selects, in one DuckDB join per source."""
    by_source: dict[str, list[dict]] = {s: [] for s in SOURCES}
    for s in set(selectors):
        x = Selector.from_json(s)
        by_source[x.source].append({"app_uri": x.app_uri, "key": x.key,
                                    "t_start": x.t_start, "t_end": x.t_end})
    schema = {"app_uri": pl.String, "key": pl.String,
              "t_start": pl.Datetime("us", "UTC"), "t_end": pl.Datetime("us", "UTC")}
    out: set[str] = set()
    con = duckdb.connect()
    try:
        for source, rows in by_source.items():
            if not rows:
                continue
            con.register("sel", pl.DataFrame(rows, schema=schema).to_arrow())
            rows_file = rows_path(proc_root, source).as_posix()
            out |= {r[0] for r in con.execute(
                f"SELECT DISTINCT r.raw_id FROM read_parquet('{rows_file}') r "
                "JOIN sel s ON r.app_uri IS NOT DISTINCT FROM s.app_uri "
                "AND (s.key IS NULL OR r.key = s.key) "
                "AND ((s.t_start = s.t_end AND r.ts_utc IS NULL) "
                "OR (r.ts_utc >= s.t_start AND r.ts_utc < s.t_end))").fetchall()}
            con.unregister("sel")
    finally:
        con.close()
    return out
