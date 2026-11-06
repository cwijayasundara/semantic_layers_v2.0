"""`preprocess_scenario`: raw tree -> per-source Parquet + quarantine + ledger.

Layer 1 of the entropy ladder. It does three things and changes no value:
  * Lossless split and parse. Every byte of every raw file lands in exactly
    one record span, and each span becomes parsed row(s) or one quarantine
    row. Duplicates stay as separate rows (dedupe is Task 8).
  * Time normalization. `ts_raw` is kept as written, and `ts_utc` is the
    UTC instant corrected for the estimated clock skew.
  * Proof. `check_coverage` re-reads the raw bytes and the Parquet raw ids.
    `ledger.json` records the counts, zone hints, skew and coverage.

Files stream one at a time. Logs and spans are written in a first pass,
because skew needs request-id pairs across all hosts; a streaming second
pass then applies `skew_ms`. Pipeline code never reads `_answers/` (R5) and
never imports the generator (R6)."""
import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from e2ekg.preprocess.ledger import check_coverage
from e2ekg.preprocess.parsers import PARSERS, UNKNOWN, Truncated, detect_format
from e2ekg.preprocess.tables import (
    QUARANTINE_COLS,
    REASONS,
    TABLES,
    Sink,
    dumps,
    path_cols,
    route,
    table_schema,
)
from e2ekg.preprocess.timefix import (
    estimate_skew,
    infer_tz,
    is_naive,
    span_skew,
    to_epoch_us,
)
from e2ekg.rawid import raw_id

__all__ = ["TABLES", "PreprocessReport", "preprocess_scenario"]

HEAD_BYTES = 64 * 1024
EXCERPT_MAX = 65_536
SKEWED_TABLES = ("logs", "spans")
# Files whose stamps may lack a zone are parsed after the zone-aware ones,
# so `infer_tz` has a reference clock. Order affects nothing else.
_NAIVE_PRONE = frozenset({"log4j"})
_REFERENCE_SOURCES = frozenset({"logs", "traces"})


@dataclass
class PreprocessReport:
    scenario: str
    files: int = 0
    bytes_total: int = 0
    records: dict[str, int] = field(default_factory=dict)      # raw-id source -> records
    tables: dict[str, int] = field(default_factory=dict)       # table -> rows
    quarantine: dict[str, int] = field(default_factory=dict)   # reason -> rows
    formats: dict[str, int] = field(default_factory=dict)      # format -> files
    tz_hints: dict[str, str] = field(default_factory=dict)     # rel_path -> zone (naive files)
    ts_unparsed: int = 0
    skew: dict[str, dict[str, int]] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0


def _excerpt(payload: bytes) -> str:
    return payload[:EXCERPT_MAX].decode("utf-8", errors="backslashreplace")


class _Run:
    def __init__(self, raw_root: Path, out: Path) -> None:
        self.raw_root, self.out = raw_root, out
        self.report = PreprocessReport(scenario=raw_root.name)
        self.sinks: dict[str, Sink] = {}
        self.quarantine = Sink(out / "quarantine.parquet", QUARANTINE_COLS)
        self.reference: list[int] = []  # per-file median of zone-aware stamps (µs)

    def sink(self, table: str) -> Sink:
        if table not in self.sinks:
            name = f"{table}.pass1.parquet" if table in SKEWED_TABLES else f"{table}.parquet"
            self.sinks[table] = Sink(self.out / name, table_schema(table))
        return self.sinks[table]

    def quarantine_row(self, rid: str, source: str, rel: str, reason: str, payload: bytes,
                       fmt: str, detail: str) -> None:
        self.quarantine.add({"raw_id": rid, "source": source, "rel_path": rel, "reason": reason,
                             "excerpt": _excerpt(payload), "format": fmt,
                             "detail": detail[:500]})
        self.report.quarantine[reason] = self.report.quarantine.get(reason, 0) + 1

    def file(self, rel: str, fmt: str) -> None:
        data = (self.raw_root / rel).read_bytes()
        source, table = route(rel)
        self.report.files += 1
        self.report.bytes_total += len(data)
        self.report.formats[fmt] = self.report.formats.get(fmt, 0) + 1
        if not data:
            return
        if fmt == UNKNOWN or table is None:
            self.quarantine_row(raw_id(source, rel, 0, data), source, rel, "unknown_format",
                                data, fmt, "no parser recognises this file")
            self._count(source)
            return
        parser = PARSERS[fmt]
        if hasattr(parser, "for_file"):
            parser = parser.for_file(data)
        pcols = path_cols(table, rel) | ({"log_format": fmt} if table == "logs" else {})
        rows: list[dict] = []
        for off, n in parser.split(data):
            payload = data[off:off + n]
            rid = raw_id(source, rel, off, payload)
            self._count(source)
            reason, detail = None, ""
            if not payload.strip():
                reason, detail = "unparseable", "blank record"
            else:
                try:
                    parsed = parser.parse(payload)
                except Truncated as e:
                    reason, detail = "truncated", str(e)
                except UnicodeDecodeError as e:
                    reason, detail = "decode_error", str(e)
                except Exception as e:  # any failure must quarantine, never drop
                    reason, detail = "unparseable", f"{type(e).__name__}: {e}"
            if reason:
                self.quarantine_row(rid, source, rel, reason, payload, fmt, detail)
                continue
            base = {"raw_id": rid, "source": source, "rel_path": rel, "skew_ms": 0}
            for r in parsed.get("rows") or [parsed]:
                rows.append(base | pcols | r["cols"] | {
                    "ts_raw": r["ts_raw"],
                    "surface_app": r["surface_app"] or pcols.get("app_dir"),
                    "fields": dumps(r["fields"]), "_residual": dumps(r["_residual"])})
        self._timestamps(rel, source, table, rows)
        out = self.sink(table)
        for r in rows:
            out.add(r)

    def _count(self, source: str) -> None:
        self.report.records[source] = self.report.records.get(source, 0) + 1

    def _timestamps(self, rel: str, source: str, table: str, rows: list[dict]) -> None:
        """ts_utc for a file's rows; one zone hint per file for naive stamps."""
        flags = [r["ts_raw"] is not None and is_naive(r["ts_raw"]) for r in rows]
        naive = [r["ts_raw"] for r, f in zip(rows, flags, strict=True) if f]
        hint = None
        if naive and self.reference:
            hint = infer_tz(sorted(naive)[len(naive) // 2], int(statistics.median(self.reference)))
        if naive:
            self.report.tz_hints[rel] = hint or "UTC"
        aware: list[int] = []
        for r, is_naive_ts in zip(rows, flags, strict=True):
            raw = r["ts_raw"]
            us = None
            if raw is not None:
                try:
                    us = to_epoch_us(raw, hint)
                except (ValueError, OverflowError):
                    self.report.ts_unparsed += 1
            r["ts_utc"] = us
            if table == "logs":
                r["tz_hint"] = (hint or "UTC") if is_naive_ts else None
            if table == "spans" and us is not None and r.get("duration_us") is not None:
                r["end_utc"] = us + r["duration_us"]
            if us is not None and source in _REFERENCE_SOURCES and not naive:
                aware.append(us)
        if aware:
            self.reference.append(int(statistics.median(aware)))

    def close(self) -> None:
        for t in TABLES:
            self.sink(t)
        for s in self.sinks.values():
            s.close()
        self.quarantine.close()


def _apply_skew(out: Path, table: str, skew: dict[str, int]) -> int:
    """Second pass: stream pass-1 rows, set skew_ms, correct ts_utc (and end_utc)."""
    src, dst = out / f"{table}.pass1.parquet", out / f"{table}.parquet"
    nonzero = {h: v for h, v in skew.items() if v}
    lf = pl.scan_parquet(src)
    skew_ms = (pl.col("host").replace_strict(nonzero, default=0, return_dtype=pl.Int64)
               if nonzero else pl.lit(0, dtype=pl.Int64))
    lf = lf.with_columns(skew_ms=skew_ms).with_columns(
        *(pl.col(c) - pl.duration(milliseconds=pl.col("skew_ms"))
          for c in (("ts_utc", "end_utc") if table == "spans" else ("ts_utc",))))
    lf.sink_parquet(dst, compression="zstd")
    src.unlink()
    return pl.scan_parquet(dst).select(pl.len()).collect().item()


def _skew_inputs(out: Path) -> tuple[pl.DataFrame, pl.DataFrame]:
    logs = (pl.scan_parquet(out / "logs.pass1.parquet")
            .filter(pl.col("request_id").is_not_null())
            .select("host", "request_id", "msg", "ts_utc").collect())
    spans = (pl.scan_parquet(out / "spans.pass1.parquet")
             .select("trace_id", "span_id", "parent_span_id", "host", "ts_utc").collect())
    hosts = pl.scan_parquet(out / "logs.pass1.parquet").select(
        pl.col("host").unique()).collect()
    logs = pl.concat([logs, hosts.with_columns(request_id=pl.lit(None, pl.String),
                                               msg=pl.lit(None, pl.String),
                                               ts_utc=pl.lit(None, logs["ts_utc"].dtype))],
                     how="vertical_relaxed")
    return spans, logs


def preprocess_scenario(raw_root: Path, processed_root: Path, llm: Any = None) -> PreprocessReport:
    """`llm` is unused at layer 1; Tasks 7-9 extend this entry point."""
    t0 = time.perf_counter()
    raw_root, out = Path(raw_root), Path(processed_root)
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.parquet"):
        stale.unlink()
    plan = []
    for p in sorted(raw_root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(raw_root).as_posix()
            with p.open("rb") as fh:
                plan.append((rel, detect_format(rel, fh.read(HEAD_BYTES))))
    plan.sort(key=lambda rf: (rf[1] in _NAIVE_PRONE, rf[0]))

    run = _Run(raw_root, out)
    for rel, fmt in plan:
        run.file(rel, fmt)
    run.close()
    report = run.report

    spans_df, logs_df = _skew_inputs(out)
    report.skew = {"logs": estimate_skew(spans_df, logs_df), "spans": span_skew(spans_df)}
    del spans_df, logs_df
    report.tables = {t: run.sinks[t].rows for t in TABLES}
    for t in SKEWED_TABLES:
        report.tables[t] = _apply_skew(out, t, report.skew[t])
    (out / "clock_skew.json").write_text(json.dumps(report.skew, indent=1, sort_keys=True))

    cov = check_coverage(raw_root, out)
    report.coverage = {"files": cov.files, "bytes_total": cov.bytes_total,
                       "bytes_covered": cov.bytes_covered, "gaps": len(cov.gaps),
                       "overlaps": len(cov.overlaps), "mismatches": len(cov.mismatches),
                       "first_gaps": [g.raw_id for g in cov.gaps[:20]],
                       "first_overlaps": [o.raw_id for o in cov.overlaps[:20]]}
    report.quarantine = {r: report.quarantine.get(r, 0) for r in REASONS}
    report.seconds = round(time.perf_counter() - t0, 2)
    (out / "ledger.json").write_text(json.dumps(asdict(report), indent=1, sort_keys=True))
    return report
