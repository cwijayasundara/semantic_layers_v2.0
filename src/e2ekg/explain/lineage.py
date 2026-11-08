"""Raw -> processed -> graph lineage for one node: the raw lines behind it (through the
agent's own `raw_evidence`, so the as-of cutoff holds), the processed row each parsed into,
and the named graph the node lives in. With `as_of` (the tools' cutoff) the processed row is
read through `asof.scan`, so a log line shows the template mined at the cutoff, as the as-of
store does, not the full window's."""
import json
from datetime import datetime
from pathlib import Path

import polars as pl

from e2ekg.graph.uris import BASE
from e2ekg.preprocess import asof as _asof
from e2ekg.rawid import RawSpan

_TABLE = {"logs": "logs", "atlas": "metrics_atlas", "prom": "metrics_prom",
          "traces": "spans", "events": "events", "slack": "slack"}
_FIELDS = ("ts_raw", "skew_ms", "tz_hint", "app_method", "app_confidence", "template")
_GRAPH = BASE + "/graph/"


def _graph_tail(tools: dict, uri: str) -> str | None:
    out = tools["sparql_select"].invoke(
        {"query": f'SELECT DISTINCT ?g WHERE {{ GRAPH ?g {{ <{uri}> ?p ?o }} }}'})
    if not out.startswith("["):
        return None
    for row in json.loads(out):
        g = str(row.get("g", ""))
        if g.startswith(_GRAPH):
            return g[len(_GRAPH):]
    return None


def _processed(proc_root: Path, source: str, rid: str,
               as_of: datetime | None = None) -> dict | None:
    table = _TABLE.get(source)
    path = Path(proc_root) / f"{table}.parquet"
    if table is None or not path.exists():
        return None
    lf = _asof.scan(proc_root, table, as_of)
    have = lf.collect_schema().names()
    row = lf.filter(pl.col("raw_id") == rid).select(
        [pl.col(c) if c in have else pl.lit(None).alias(c) for c in _FIELDS]).head(1).collect()
    if row.height == 0:
        return None
    return {"table": table, **row.row(0, named=True)}


def lineage(tools: dict, proc_root: Path, uri: str, limit: int = 3,
            as_of: datetime | None = None) -> dict:
    out = tools["raw_evidence"].invoke({"uri_or_selector": uri, "limit": limit})
    items = json.loads(out) if out.startswith("[") else []
    records = []
    for it in items:
        rid = it.get("raw_id")
        if not rid or "line" not in it:
            continue
        span = RawSpan.parse(rid)
        records.append({
            "raw_id": rid, "file": span.rel_path, "offset": span.offset,
            "length": span.length, "sha": rid.rsplit("|", 1)[1], "line": it["line"],
            "ts_utc": it.get("ts"),
            "processed": _processed(proc_root, span.source, rid, as_of)})
    return {"uri": uri, "graph": _graph_tail(tools, uri), "records": records}
