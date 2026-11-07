"""Coverage ledger: proof that every raw byte is in exactly one row.

`check_coverage` reads every `raw_id` in every Parquet table under
`processed_root` (parsed tables and quarantine) and walks every raw file:
  * gaps: byte ranges no record span covers. A single b"\\n" right after a
    span is that span's separator and counts as covered.
  * overlaps: a span starting inside an earlier one, or one raw id appearing
    in two tables (parsed and quarantined, say). The same raw id repeated
    inside one table is a single record (an OTLP line's spans).
  * mismatches: ids whose digest does not match the raw bytes at
    offset/length, or that point past the end of a file or at a missing file.
It trusts nothing but the raw bytes and the ids."""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import duckdb
import polars as pl

from e2ekg.rawid import RawSpan, raw_id

_ID = r"^([^|]+)\|(.*)\|(\d+)\|(\d+)\|([0-9a-f]{16})$"


@dataclass
class CoverageReport:
    files: int
    bytes_total: int
    bytes_covered: int
    gaps: list[RawSpan]
    overlaps: list[RawSpan]
    mismatches: list[RawSpan] = field(default_factory=list)


def _gap(rel: str, off: int, chunk: bytes) -> RawSpan:
    return RawSpan.parse(raw_id("gap", rel, off, chunk))


def _ids(processed_root: Path) -> pl.DataFrame:
    frames = []
    for p in sorted(processed_root.glob("*.parquet")):
        lf = pl.scan_parquet(p)
        if "raw_id" in lf.collect_schema().names():
            frames.append(lf.select("raw_id").unique().with_columns(table=pl.lit(p.stem)))
    if not frames:
        return pl.DataFrame(schema={"raw_id": pl.String, "table": pl.String})
    return pl.concat(frames).collect()


def check_coverage(raw_root: Path, processed_root: Path) -> CoverageReport:
    raw_root, processed_root = Path(raw_root), Path(processed_root)
    ids = _ids(processed_root)
    overlaps: list[RawSpan] = []
    multi = ids.group_by("raw_id").agg(pl.col("table").n_unique().alias("k")).filter(
        pl.col("k") > 1)
    overlaps += [RawSpan.parse(r) for r in sorted(multi["raw_id"])]

    spans = ids.select("raw_id").unique().with_columns(
        pl.col("raw_id").str.extract_groups(_ID).alias("g")).unnest("g").rename(
        {"1": "source", "2": "rel_path", "3": "offset", "4": "length", "5": "digest"}).with_columns(
        pl.col("offset").cast(pl.Int64), pl.col("length").cast(pl.Int64))
    mismatches = [RawSpan(raw_id=r, source="?", rel_path="?", offset=-1, length=-1)
                  for r in spans.filter(pl.col("rel_path").is_null())["raw_id"]]
    spans = spans.filter(pl.col("rel_path").is_not_null()).sort("rel_path", "offset", "length")
    by_file = {k[0]: v for k, v in spans.partition_by("rel_path", as_dict=True).items()}

    files = sorted(p for p in raw_root.rglob("*") if p.is_file())
    seen_rel = set()
    gaps: list[RawSpan] = []
    bytes_total = bytes_covered = 0
    for p in files:
        rel = p.relative_to(raw_root).as_posix()
        seen_rel.add(rel)
        data = p.read_bytes()
        bytes_total += len(data)
        pos = 0  # first byte not yet covered
        df = by_file.get(rel)
        rows = df.select("raw_id", "offset", "length", "digest").iter_rows() if df is not None \
            else ()
        for rid, off, n, digest in rows:
            end = off + n
            if end > len(data) or hashlib.sha256(data[off:end]).hexdigest()[:16] != digest:
                mismatches.append(RawSpan.parse(rid))
                continue
            if off < pos:
                overlaps.append(RawSpan.parse(rid))
                bytes_covered += max(0, end - pos)
            else:
                if off > pos:
                    gaps.append(_gap(rel, pos, data[pos:off]))
                bytes_covered += n
            pos = max(pos, end)
            if data[pos:pos + 1] == b"\n":  # the separator after this span
                bytes_covered += 1
                pos += 1
        if pos < len(data):
            gaps.append(_gap(rel, pos, data[pos:]))
    mismatches += [RawSpan.parse(r) for rel, df in by_file.items() if rel not in seen_rel
                   for r in df["raw_id"]]
    return CoverageReport(files=len(files), bytes_total=bytes_total, bytes_covered=bytes_covered,
                          gaps=gaps, overlaps=overlaps, mismatches=mismatches)


# -- reachability ---------------------------------------------------------------
# Every parsed row must reach the graph: either a fingerprint's selector selects it
# (logs, metrics, spans), or the row becomes a node directly in Task 12 (events,
# alerts, pages, incidents, catalog and chat rows). Quarantine rows are not parsed rows;
# the byte ledger above already accounts for them.
DIRECT_TABLES = ("events", "alerts", "pages", "incidents", "catalog", "slack")
SELECTED = {"logs": ("logs",), "metrics": ("metrics_atlas", "metrics_prom"), "spans": ("spans",)}


@dataclass
class ReachabilityReport:
    rows_total: int
    rows_reachable: int
    unreachable_raw_ids: list[str]
    # raw ids a selector selects that are not in the parsed tables (a deleted row)
    phantom_raw_ids: list[str] = field(default_factory=list)
    by_table: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.unreachable_raw_ids and not self.phantom_raw_ids and (
            self.rows_reachable == self.rows_total)


def _selectors(fingerprint_tables: dict[str, pl.DataFrame]) -> dict[str, pl.DataFrame]:
    by_source: dict[str, list[dict]] = {}
    for df in fingerprint_tables.values():
        if "selector" not in df.columns:
            continue
        for s in df["selector"].unique().to_list():
            d = json.loads(s)
            by_source.setdefault(d["source"], []).append({
                "app_uri": d["app_uri"], "key": d["key"],
                "t_start": datetime.fromisoformat(d["t_start"]),
                "t_end": datetime.fromisoformat(d["t_end"])})
    schema = {"app_uri": pl.String, "key": pl.String,
              "t_start": pl.Datetime("us", "UTC"), "t_end": pl.Datetime("us", "UTC")}
    return {src: pl.DataFrame(by_source.get(src, []), schema=schema) for src in SELECTED}


def check_reachability(proc_root: Path, fingerprint_tables: dict[str, pl.DataFrame]
                       ) -> ReachabilityReport:
    proc = Path(proc_root)
    sels = _selectors(fingerprint_tables)
    con = duckdb.connect()
    total = reachable = 0
    unreachable: list[str] = []
    phantom: list[str] = []
    by_table: dict[str, dict[str, int]] = {}

    def count(table: str) -> int:
        path = (proc / f"{table}.parquet").as_posix()
        return con.execute(f"SELECT count(*) FROM read_parquet('{path}')").fetchall()[0][0]

    for t in DIRECT_TABLES:
        n = count(t)
        total += n
        reachable += n
        by_table[t] = {"rows": n, "reachable": n}

    for source, tables in SELECTED.items():
        spans = source == "spans"
        ident = "raw_id || '#' || CAST(span_index AS VARCHAR)" if spans else "raw_id"
        actual = " UNION ALL ".join(
            f"SELECT {ident} AS id, raw_id FROM read_parquet('{proc.as_posix()}/{t}.parquet')"
            for t in tables)
        rows = (proc / "derived" / f"rows_{source}.parquet").as_posix()
        con.register("sel", sels[source].to_arrow())
        got = (f"SELECT DISTINCT {ident} AS id FROM read_parquet('{rows}') r JOIN sel s "
               "ON r.app_uri IS NOT DISTINCT FROM s.app_uri AND (s.key IS NULL OR r.key = s.key) "
               "AND ((s.t_start = s.t_end AND r.ts_utc IS NULL) "
               "OR (r.ts_utc >= s.t_start AND r.ts_utc < s.t_end))")
        con.execute(f"CREATE OR REPLACE TEMP TABLE actual AS {actual}")
        con.execute(f"CREATE OR REPLACE TEMP TABLE got AS {got}")
        n = con.execute("SELECT count(*) FROM actual").fetchall()[0][0]
        miss = [r[0] for r in con.execute(
            "SELECT DISTINCT raw_id FROM actual WHERE id NOT IN (SELECT id FROM got) "
            "ORDER BY raw_id").fetchall()]
        n_miss = con.execute("SELECT count(*) FROM actual WHERE id NOT IN (SELECT id FROM got)"
                         ).fetchall()[0][0]
        ghost = [r[0] for r in con.execute(
            "SELECT DISTINCT split_part(id, '#', 1) FROM got WHERE id NOT IN "
            "(SELECT id FROM actual) ORDER BY 1").fetchall()]
        total += n
        reachable += n - n_miss
        unreachable += miss
        phantom += ghost
        by_table[source] = {"rows": n, "reachable": n - n_miss}
    con.close()
    return ReachabilityReport(total, reachable, unreachable, phantom, by_table)
