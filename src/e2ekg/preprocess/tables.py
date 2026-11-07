"""Output tables: path routing, Arrow schemas and streaming Parquet sinks.

Every table starts with the ParsedRow common columns:
  raw_id, source, rel_path, ts_raw, ts_utc, skew_ms, surface_app, fields, _residual
`fields` and `_residual` are JSON text, because records within one table
differ in shape. Table-specific columns follow. `ts_utc` is the corrected
instant, so the instant as written is `ts_utc + skew_ms`.

The raw-id `source` names the raw source families (catalog, atlas, prom,
logs, traces, events, chat); the raw tree's directory layout maps onto them."""
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

TS = pa.timestamp("us", tz="UTC")
COMMON: list[tuple[str, pa.DataType]] = [
    ("raw_id", pa.string()), ("source", pa.string()), ("rel_path", pa.string()),
    ("ts_raw", pa.string()), ("ts_utc", TS), ("skew_ms", pa.int64()),
    ("surface_app", pa.string()), ("fields", pa.string()), ("_residual", pa.string()),
]
S, F, I64 = pa.string(), pa.float64(), pa.int64()
_EVENT = [("event_kind", S), ("row_kind", S), ("external_id", S), ("event_type", S),
          ("status", S)]
TABLE_COLS: dict[str, list[tuple[str, pa.DataType]]] = {
    "catalog": [("app_id", S), ("app_kind", S)],
    "metrics_atlas": [("metric", S), ("tags", S), ("value", F)],
    "metrics_prom": [("prom_kind", S), ("metric", S), ("labels", S), ("value", F),
                     ("value_raw", S), ("metric_type", S), ("help", S), ("instance", S)],
    "logs": [("host", S), ("app_dir", S), ("log_format", S), ("level", S), ("logger", S),
             ("thread", S), ("msg", S), ("request_id", S), ("tz_hint", S)],
    "spans": [("span_index", pa.int32()), ("span_format", S), ("host", S), ("trace_id", S),
              ("span_id", S), ("parent_span_id", S), ("name", S), ("span_kind", S),
              ("duration_us", I64), ("end_utc", TS), ("status_code", S), ("attributes", S)],
    "events": _EVENT,
    "alerts": [("alert_id", S), ("severity", S), ("state", S), ("metric", S)],
    "pages": [("page_id", S), ("status", S), ("urgency", S), ("title", S)],
    "incidents": [("incident_id", S), ("event", S), ("status", S), ("severity", S)],
    "slack": [("channel", S), ("user", S), ("text", S), ("subtype", S), ("thread_ts", S)],
}
TABLES = tuple(TABLE_COLS)
JSON_COLS = frozenset({"fields", "_residual", "tags", "labels", "attributes"})
QUARANTINE_COLS: list[tuple[str, pa.DataType]] = [
    ("raw_id", S), ("source", S), ("rel_path", S), ("reason", S), ("excerpt", S),
    ("format", S), ("detail", S),
]
REASONS = ("decode_error", "truncated", "unparseable", "unknown_format")

# (path prefix, raw-id source, table). Longest prefix first.
_ROUTES = (
    ("catalog/", "catalog", "catalog"),
    ("metrics/prom/", "prom", "metrics_prom"),
    ("metrics/", "atlas", "metrics_atlas"),
    ("logs/", "logs", "logs"),
    ("traces/", "traces", "spans"),
    ("events/", "events", "events"),
    ("alerts/", "events", "alerts"),
    ("pagerduty/", "events", "pages"),
    ("incidents/", "events", "incidents"),
    ("slack/", "chat", "slack"),
)


def route(rel_path: str) -> tuple[str, str | None]:
    """(raw-id source, table) for a raw file; table None means no known source."""
    for prefix, source, tbl in _ROUTES:
        if rel_path.startswith(prefix):
            return source, tbl
    return rel_path.split("/", 1)[0] or "unknown", None


def path_cols(table: str, rel_path: str) -> dict[str, Any]:
    parts = rel_path.split("/")
    if table == "logs":
        return {"host": parts[-1].rsplit(".", 1)[0], "app_dir": parts[1] if len(parts) > 2
                else None}
    if table == "slack":
        return {"channel": parts[1] if len(parts) > 2 else None}
    return {}


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str,
                      allow_nan=True)


class Sink:
    """Buffers rows column-wise and flushes Arrow record batches to Parquet,
    so a file's rows are never all held as Python dicts at once."""

    FLUSH_ROWS = 65_536

    def __init__(self, path: Path, cols: list[tuple[str, pa.DataType]]) -> None:
        self.path = path
        self.schema = pa.schema(cols)
        self.names = [n for n, _ in cols]
        self.buf: dict[str, list] = {n: [] for n in self.names}
        self.rows = 0
        self._writer: pq.ParquetWriter | None = None

    def add(self, values: dict[str, Any]) -> None:
        for n in self.names:
            v = values.get(n)
            if n in JSON_COLS and v is not None and not isinstance(v, str):
                v = dumps(v)
            self.buf[n].append(v)
        self.rows += 1
        if len(self.buf[self.names[0]]) >= self.FLUSH_ROWS:
            self.flush()

    def flush(self) -> None:
        if not self.buf[self.names[0]] and self._writer is not None:
            return
        arrays = [pa.array(self.buf[n], type=t) for n, t in zip(self.names, self.schema.types,
                                                                 strict=True)]
        if self._writer is None:
            self._writer = pq.ParquetWriter(self.path, self.schema, compression="zstd")
        self._writer.write_table(pa.Table.from_arrays(arrays, schema=self.schema))
        self.buf = {n: [] for n in self.names}

    def close(self) -> None:
        self.flush()
        if self._writer is not None:
            self._writer.close()


def table_schema(table: str) -> list[tuple[str, pa.DataType]]:
    return COMMON + TABLE_COLS[table]
