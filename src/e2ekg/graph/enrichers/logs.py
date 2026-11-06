"""Log enricher: ops:LogPattern per (application, Drain template), ops:LogSpike per
detected burst, each classified into the closed error category scheme.

  * LogPattern: every (app, template_id) of `derived/rows_logs`, so the pattern
    selectors together reach EVERY log row. log:template, ops:onApplication,
    ops:count, ops:atTime (first seen) = ops:windowStart, ops:windowEnd,
    ops:errorCategory (rules below; only for WARN+ templates or a rule hit) and
    ops:rawSelector (one selector over the timed rows, plus the `t_start ==
    t_end` sentinel selector when the pattern has rows with no time).
  * LogSpike: every row of `derived/fp_log_spikes`: ops:ofPattern,
    ops:onApplication, ops:atTime = ops:windowStart, ops:windowEnd, ops:count,
    ops:magnitude (peak robust z) and the spike's ops:rawSelector.
  * Access-log refinement (extension log:refines / log:httpStatus): Drain folds
    an nginx line's status into a variable ("POST <*> <*>"), so a 504 burst on
    edge-gateway hides inside the 2xx/4xx traffic of its template. A template
    that starts with an HTTP method and whose last parameter is a 3-digit status
    is split by 5xx status into refined patterns ("POST <*> 504"), each with its
    own category and a spike search over 5-minute buckets (robust z >= 6 against
    the trailing hour, >= 10 lines, extended while above the onset threshold).
    Selectors key on the template, not the status, so a refined pattern or spike
    cites its parent template's selector (a superset of its rows; its ops:count
    is the refined count). This belongs in preprocessing's template miner; it is
    done here until that moves.

Error categories (first rule wins; WARN/ERROR/FATAL templates that match none are
`other`; lower levels that match none get no category)."""
import re
from datetime import timedelta

import numpy as np
import polars as pl
from rdflib import URIRef
from rdflib.namespace import RDF

from e2ekg.graph.enricher import NS, Enricher, dec, finite, integer, node, read_table, string, ts
from e2ekg.graph.uris import concept
from e2ekg.preprocess import selector as sel

OPS, LOG = NS["ops"], NS["log"]
_RULES = (
    ("pool_exhaustion", r"pool exhausted|RESOURCE_EXHAUSTED|no idle connections?"),
    ("timeout", r"deadline exceeded|DEADLINE_EXCEEDED|timed? ?out\b|timeout|^\S+ <\*> 504$"),
    ("partial_response", r"partial response|partially failed"),
    ("null_pointer", r"NullPointerException|null pointer|NoneType"),
    ("tls_error", r"certificate|x509|\bTLS\b|\bSSL\b|handshake"),
    ("cache_miss", r"cache miss|evict"),
    ("hot_partition", r"hot partition|hot key"),
    ("retry_exhausted", r"retr(?:y|ies) (?:budget )?exhausted|giving up after"),
    ("load_shed", r"\bshed|rejected execution|no healthy hosts|^\S+ <\*> 503$"),
    ("server_error", r"internal server error|exception|\bfailed\b|^\S+ <\*> 5\d\d$"),
)
_RULES_RX = tuple((c, re.compile(p, re.I)) for c, p in _RULES)
_ERRORISH = {"WARN", "WARNING", "ERROR", "SEVERE", "CRITICAL", "FATAL"}
_METHOD = re.compile(r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS) ")
_STATUS = re.compile(r"^[1-5]\d\d$")
BUCKET = timedelta(minutes=5)
Z_MIN, COUNT_MIN, BASE_BUCKETS, MISS_MAX = 6.0, 10, 12, 2


def error_category(template: str, level: str | None) -> str | None:
    for cat, rx in _RULES_RX:
        if rx.search(template or ""):
            return cat
    return "other" if (level or "").upper() in _ERRORISH else None


def access_status(template: str, params) -> int | None:
    """HTTP status of an access-log line, from its last template parameter."""
    if not template or not _METHOD.match(template) or not template.endswith("<*>"):
        return None
    if params is None or len(params) == 0:
        return None
    last = str(params[-1])
    return int(last) if _STATUS.match(last) else None


def bursts(counts: np.ndarray) -> list[tuple[int, int, float]]:
    """(first, last, peak z) bucket runs: onset when z >= Z_MIN against the trailing
    BASE_BUCKETS (at least 3 of them) and count >= COUNT_MIN; the onset baseline
    is kept while the run lasts (up to MISS_MAX buckets below it)."""
    out, n, i = [], len(counts), 0
    while i < n:
        lo = max(0, i - BASE_BUCKETS)
        base = counts[lo:i]
        if len(base) < 3:
            i += 1
            continue
        med = float(np.median(base))
        sigma = max(1.4826 * float(np.median(np.abs(base - med))), float(np.sqrt(med)), 1.0)
        thr = med + Z_MIN * sigma
        if counts[i] < max(COUNT_MIN, thr):
            i += 1
            continue
        j, m, miss = i, i + 1, 0
        while m < n and miss <= MISS_MAX:
            if counts[m] >= thr:
                j, miss = m, 0
            else:
                miss += 1
            m += 1
        out.append((i, j, float((counts[i:j + 1].max() - med) / sigma)))
        i = j + 1
    return out


class LogPatternEnricher(Enricher):
    name = "logs"
    reads = ("derived/rows_logs", "derived/fp_log_buckets", "derived/fp_log_spikes", "logs")

    def fetch(self, ctx):
        rows = pl.scan_parquet(ctx.proc_root / "derived" / "rows_logs.parquet")
        timed = (rows.filter(pl.col("ts_utc").is_not_null()).group_by("app_uri", "key")
                 .agg(pl.len().alias("n"), pl.col("ts_utc").min().alias("t0"),
                      pl.col("ts_utc").max().alias("t1")))
        untimed = (rows.filter(pl.col("ts_utc").is_null()).group_by("app_uri", "key")
                   .agg(pl.len().alias("n_untimed")))
        pats = (timed.join(untimed, on=["app_uri", "key"], how="full", coalesce=True,
                           nulls_equal=True)
                .collect().sort("app_uri", "key", nulls_last=True))
        levels = (read_table(ctx, "derived/fp_log_buckets", ["app_uri", "template_id",
                                                             "error_level"])
                  .unique().to_dicts())
        logs = pl.scan_parquet(ctx.proc_root / "logs.parquet")
        text = (logs.select("template_id", "template").unique("template_id", keep="first")
                .collect().sort("template_id"))
        access_ids = [t for t, x in text.iter_rows() if x and _METHOD.match(x)
                      and x.endswith("<*>")]
        access = (logs.filter(pl.col("template_id").is_in(access_ids))
                  .select("app_uri", "template_id", "template", "ts_utc", "params").collect())
        spikes = read_table(ctx, "derived/fp_log_spikes").sort("t_start", "app_uri",
                                                              "template_id").to_dicts()
        return pats, levels, dict(text.iter_rows()), access, spikes

    def analyze(self, raw):
        pats, levels, text, access, spikes = raw
        rank = {"DEBUG": 1, "INFO": 2, "WARN": 3, "ERROR": 4, "FATAL": 5}
        level: dict[tuple, str] = {}
        for r in levels:
            k = (r["app_uri"], r["template_id"])
            if rank.get(r["error_level"] or "", 0) > rank.get(level.get(k, ""), 0):
                level[k] = r["error_level"]
        patterns = []
        for r in pats.to_dicts():
            tpl = text.get(r["key"], "")
            lv = level.get((r["app_uri"], r["key"]))
            patterns.append({**r, "template": tpl, "level": lv,
                             "category": error_category(tpl, lv)})
        refined, refined_spikes = _refine(access)
        return patterns, spikes, refined, refined_spikes

    def emit(self, result):
        patterns, spikes, refined, refined_spikes = result
        sc = self.scenario
        for p in patterns:
            u = node("log", "pattern", sc, p["app_uri"], p["key"])
            yield u, RDF.type, OPS.LogPattern
            yield u, LOG.template, string(p["template"])
            if p["app_uri"]:
                yield u, OPS.onApplication, URIRef(p["app_uri"])
            yield u, OPS["count"], integer((p["n"] or 0) + (p["n_untimed"] or 0))
            if p["category"]:
                yield u, OPS.errorCategory, concept("error_category", p["category"])
            if p["t0"] is not None:
                yield u, OPS.atTime, ts(p["t0"])
                yield u, OPS.windowStart, ts(p["t0"])
                end = p["t1"] + timedelta(microseconds=1)
                yield u, OPS.windowEnd, ts(end)
                yield u, OPS.rawSelector, string(sel.make("logs", p["app_uri"], p["key"],
                                                          p["t0"], end))
            if p["n_untimed"]:
                yield u, OPS.rawSelector, string(sel.make("logs", p["app_uri"], p["key"],
                                                          None, None))
        for s in spikes:
            u = node("log", "spike", sc, s["app_uri"], s["template_id"], s["t_start"])
            yield from _spike(u, node("log", "pattern", sc, s["app_uri"], s["template_id"]),
                              s["app_uri"], s["t_start"], s["t_end"], s["count"], s["z"],
                              s["selector"])
        for r in refined:
            u = node("log", "pattern", sc, r["app_uri"], r["template_id"], r["status"])
            yield u, RDF.type, OPS.LogPattern
            yield u, LOG.refines, node("log", "pattern", sc, r["app_uri"], r["template_id"])
            yield u, LOG.httpStatus, integer(r["status"])
            yield u, LOG.template, string(r["template"])
            if r["app_uri"]:
                yield u, OPS.onApplication, URIRef(r["app_uri"])
            yield u, OPS["count"], integer(r["n"])
            yield u, OPS.errorCategory, concept("error_category", r["category"])
            yield u, OPS.atTime, ts(r["t0"])
            yield u, OPS.windowStart, ts(r["t0"])
            yield u, OPS.windowEnd, ts(r["t1"])
            yield u, OPS.rawSelector, string(sel.make("logs", r["app_uri"], r["template_id"],
                                                      r["t0"], r["t1"]))
        for s in refined_spikes:
            pat = node("log", "pattern", sc, s["app_uri"], s["template_id"], s["status"])
            u = node("log", "spike", sc, s["app_uri"], s["template_id"], s["status"], s["t_start"])
            yield from _spike(u, pat, s["app_uri"], s["t_start"], s["t_end"], s["count"], s["z"],
                              sel.make("logs", s["app_uri"], s["template_id"], s["t_start"],
                                       s["t_end"]))


def _spike(u, pattern, app, t0, t1, count, z, selector):
    yield u, RDF.type, OPS.LogSpike
    yield u, OPS.ofPattern, pattern
    if app:
        yield u, OPS.onApplication, URIRef(app)
    yield u, OPS.atTime, ts(t0)
    yield u, OPS.windowStart, ts(t0)
    yield u, OPS.windowEnd, ts(t1)
    yield u, OPS["count"], integer(count)
    if finite(z):
        yield u, OPS.magnitude, dec(round(float(z), 6))
    yield u, OPS.rawSelector, string(selector)


def _refine(access: pl.DataFrame) -> tuple[list[dict], list[dict]]:
    if access.height == 0:
        return [], []
    st = access.with_columns(status=pl.col("params").list.last()).filter(
        pl.col("status").str.contains(r"^5\d\d$") & pl.col("ts_utc").is_not_null()
        & pl.col("template").str.contains(r"<\*>$")
    ).with_columns(pl.col("status").cast(pl.Int64))
    if st.height == 0:
        return [], []
    t_min = access["ts_utc"].min()
    t_max = access["ts_utc"].max()
    nb = int((t_max - t_min) / BUCKET) + 1
    patterns, spikes = [], []
    for (app, tid, status), g in st.sort("app_uri", "template_id", "status", "ts_utc",
                                         nulls_last=True).group_by(
            ["app_uri", "template_id", "status"], maintain_order=True):
        tpl = g["template"][0]
        rtpl = tpl[: -len("<*>")] + str(status)
        cat = error_category(rtpl, "ERROR")
        t0, t1 = g["ts_utc"].min(), g["ts_utc"].max() + timedelta(microseconds=1)
        patterns.append({"app_uri": app, "template_id": tid, "status": int(status),
                         "template": rtpl, "category": cat, "n": g.height, "t0": t0, "t1": t1})
        idx = ((g["ts_utc"] - t_min) / BUCKET).cast(pl.Int64).to_numpy()
        counts = np.bincount(idx, minlength=nb).astype(float)
        for i, j, z in bursts(counts):
            ws, we = t_min + i * BUCKET, t_min + (j + 1) * BUCKET
            spikes.append({"app_uri": app, "template_id": tid, "status": int(status),
                           "t_start": ws, "t_end": we, "count": int(counts[i:j + 1].sum()),
                           "z": z})
    return patterns, spikes
