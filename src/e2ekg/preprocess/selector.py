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


# The one definition of "selector s selects row r". select, selected_raw_ids and
# selected_counts all join through it, so a count check proves what select() returns.
_MATCHES = ("r.app_uri IS NOT DISTINCT FROM s.app_uri AND (s.key IS NULL OR r.key = s.key) "
            "AND CASE WHEN s.t_start = s.t_end THEN r.ts_utc IS NULL "
            "ELSE r.ts_utc >= s.t_start AND r.ts_utc < s.t_end END")
_SEL_SCHEMA = {"selector": pl.String, "app_uri": pl.String, "key": pl.String,
               "t_start": pl.Datetime("us", "UTC"), "t_end": pl.Datetime("us", "UTC")}


def _join(proc_root: Path, selectors: list[str], projection: str, tail: str = "") -> list:
    """Per source, `SELECT <projection> FROM rows r JOIN selectors s ON _MATCHES <tail>`."""
    by_source: dict[str, list[dict]] = {}
    for s in sorted(set(selectors)):
        x = Selector.from_json(s)
        if x.source not in SOURCES:
            raise ValueError(f"unknown selector source {x.source!r}")
        by_source.setdefault(x.source, []).append(
            {"selector": s, "app_uri": x.app_uri, "key": x.key, "t_start": x.t_start,
             "t_end": x.t_end})
    out = []
    con = duckdb.connect()
    try:
        for source, rows in sorted(by_source.items()):
            con.register("sel", pl.DataFrame(rows, schema=_SEL_SCHEMA).to_arrow())
            # the path is interpolated; every selector value arrives as a registered column
            rows_file = rows_path(proc_root, source).as_posix()
            out.append(con.sql(f"SELECT {projection} FROM read_parquet('{rows_file}') r "
                               f"JOIN sel s ON {_MATCHES} {tail}").to_arrow_table())
            con.unregister("sel")
    finally:
        con.close()
    return out


def select(proc_root: Path, sel: Selector) -> pl.DataFrame:
    (rows,) = _join(proc_root, [sel.to_json()], "r.*")
    return pl.from_arrow(rows)


def minute_end(t: datetime) -> datetime:
    return t + timedelta(minutes=1)


def selected_raw_ids(proc_root: Path, selectors: list[str]) -> set[str]:
    """Raw ids of every row any of `selectors` (JSON) selects, in one join per source."""
    out: set[str] = set()
    for t in _join(proc_root, selectors, "DISTINCT r.raw_id"):
        out |= set(t.column("raw_id").to_pylist())
    return out


def selected_counts(proc_root: Path, selectors: list[str]) -> dict[str, int]:
    """Rows each selector (JSON) selects, zero included, in one join per source."""
    out = dict.fromkeys(selectors, 0)
    for t in _join(proc_root, selectors, "s.selector, COUNT(*) AS n", "GROUP BY s.selector"):
        out.update(zip(t.column("selector").to_pylist(), t.column("n").to_pylist(), strict=True))
    return out
