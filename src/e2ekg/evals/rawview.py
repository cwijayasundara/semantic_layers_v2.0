"""The raw tree as it was observable at a cutoff, for the raw_only arm.

The graph arms read an as-of store (R21, R34): nothing stamped at or after the cutoff reaches
them. An agent over `data/raw/<scenario>` must be held to the same line, or post-cutoff chat
and logs would name the cause for it. The view keeps, byte for byte and in file order, every
raw record the processed tables show as stamped before the cutoff (`asof.scan`, the one
definition of "observable") and drops the rest. The catalog is reference data and is kept
whole. Nothing else is cleaned, converted or re-timed: the agent still reads the sources'
own formats, time zones and skewed clocks.

Dropping bytes shifts line numbers, so `<dest>.view.json` keeps the map the scorer needs to
turn a cited `path:line` back into original byte offsets: for each file a list of
[view offset, original offset, length] segments. It sits beside the view, out of the agent's
reach."""
import json
import shutil
from datetime import datetime
from pathlib import Path

import polars as pl

from e2ekg.preprocess import asof

TABLES = ("alerts", "events", "incidents", "logs", "metrics_atlas", "metrics_prom", "pages",
          "slack", "spans")
CHUNK = 1 << 26
SEPARATOR = 2          # a raw id spans a record without its line break; <= 2 bytes (\r\n) apart
WHOLE_DIRS = ("catalog",)


def visible_ranges(proc: Path, as_of: datetime) -> dict[str, list[tuple[int, int]]]:
    """rel_path -> merged [start, end) byte ranges of the raw records observable at `as_of`.
    A raw id spans a record without the line break after it, so records at most SEPARATOR
    bytes apart share a range (the break between them is inside it)."""
    parts = []
    for t in TABLES:
        ids = asof.scan(proc, t, as_of).select("raw_id").unique().collect()["raw_id"]
        parts.append(ids.str.split("|").to_frame("p").select(
            path=pl.col("p").list.get(1), start=pl.col("p").list.get(2).cast(pl.Int64),
            n=pl.col("p").list.get(3).cast(pl.Int64)))
    recs = pl.concat(parts).unique().with_columns(end=pl.col("start") + pl.col("n")).sort(
        "path", "start")
    recs = recs.with_columns(
        prev=pl.col("end").cum_max().shift(1).over("path")).with_columns(
        block=(pl.col("prev").is_null() | (pl.col("start") > pl.col("prev") + SEPARATOR)).cum_sum().over("path"))
    merged = recs.group_by("path", "block").agg(
        pl.col("start").min(), pl.col("end").max()).sort("path", "start")
    out: dict[str, list[tuple[int, int]]] = {}
    for path, start, end in merged.select("path", "start", "end").iter_rows():
        out.setdefault(path, []).append((start, end))
    return out


def _line_break(f, at: int) -> int:
    """Bytes of the line break (\\n or \\r\\n) that follows a record ending at `at`."""
    f.seek(at)
    tail = f.read(SEPARATOR)
    if tail[:2] == b"\r\n":
        return 2
    return 1 if tail[:1] == b"\n" else 0


def build_raw_view(raw: Path, proc: Path, as_of: datetime, dest: Path) -> dict:
    """Write the view of `raw` as of `as_of` to `dest` (replacing it) and return its segment
    map, also saved as `<dest>.view.json`."""
    raw, dest = Path(raw), Path(dest)
    ranges = visible_ranges(proc, as_of)
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    view: dict[str, list[list[int]]] = {}
    for path, spans in sorted(ranges.items()):
        src = raw / path
        if not src.is_file():
            raise FileNotFoundError(f"{src}: a raw id names it, but the raw tree lacks it")
        (tmp / path).parent.mkdir(parents=True, exist_ok=True)
        pos, segments = 0, []
        with src.open("rb") as f, (tmp / path).open("wb") as out:
            for start, end in spans:
                end += _line_break(f, end)
                f.seek(start)
                for left in range(end - start, 0, -CHUNK):
                    out.write(f.read(min(CHUNK, left)))
                segments.append([pos, start, end - start])
                pos += end - start
        view[path] = segments
    for d in WHOLE_DIRS:
        for src in sorted((raw / d).rglob("*")) if (raw / d).is_dir() else ():
            if src.is_file():
                rel = src.relative_to(raw)
                (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, tmp / rel)
                view[rel.as_posix()] = [[0, 0, src.stat().st_size]]
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)
    meta = dest.with_name(dest.name + ".view.json")
    meta.write_text(json.dumps({"as_of": as_of.isoformat(), "segments": view}))
    return view


def ensure_raw_view(raw: Path, proc: Path, as_of: datetime, dest: Path) -> dict:
    """The segment map of the view at `dest`, built first if it is absent or was cut at a
    different instant."""
    meta = Path(dest).with_name(Path(dest).name + ".view.json")
    if Path(dest).is_dir() and meta.is_file():
        saved = json.loads(meta.read_text())
        if datetime.fromisoformat(saved["as_of"]) == as_of:
            return saved["segments"]
    return build_raw_view(raw, proc, as_of, dest)
