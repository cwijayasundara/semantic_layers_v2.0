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
from dataclasses import dataclass, field
from pathlib import Path

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
