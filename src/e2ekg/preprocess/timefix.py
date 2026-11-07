"""Time normalization, per-file zone inference and clock-skew estimation.

`normalize_ts` reads every timestamp spelling in the raw tree: ISO 8601 with
or without an offset, epoch s/ms/µs/ns (by digit count), Slack `s.µs`, nginx
`%d/%b/%Y:%H:%M:%S %z`, git `%a %b %d %H:%M:%S %Y %z` and log4j
`yyyy-MM-dd HH:mm:ss,SSS`. A stamp without an offset is read in
`host_tz_hint`, and in UTC when there is no hint.

Naive stamps are ambiguous. log4j files are written either in UTC or in
local (Pacific) wall-clock time. `infer_tz` picks, per file, the candidate
zone whose reading lands closest to the clock that the zone-aware records
of the same scenario agree on. The 7 h difference between the two readings
dwarfs any real skew, so the choice is unambiguous.

Clock skew. `estimate_skew` models each pair of related timestamps on two
hosts as `t_b - t_a = latency + skew_b - skew_a`. It solves for per-host
skew with robust medians (Jacobi iterations, gauge-fixed so that the median
host is 0) and does this twice:
  * on log request-id pairs (caller `Calling ... request_id=X`, callee
    `Received ... request_id=X`). These measure the log clock directly;
  * on trace parent/child spans on different hosts. These measure the
    tracer clock and are used for a host whose logs have no pairs.
Estimates under SKEW_MIN_MS, or with fewer than MIN_PAIRS pairs, are 0.
Network latency and queueing are milliseconds, so a 1 s floor separates
skew from noise. Corrected time is `ts_utc = parsed - skew_ms`."""
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl

CANDIDATE_TZS = ("UTC", "America/Los_Angeles")
SKEW_MIN_MS = 1_000
MIN_PAIRS = 5
_ITERATIONS = 4

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_US = timedelta(microseconds=1)
_MON = {m: i + 1 for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))}

_DIGITS = re.compile(r"-?\d{1,19}")
_SLACK = re.compile(r"(\d{9,11})\.(\d{1,9})")
_LOG4J = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2}),(\d{1,6})")
_NGINX = re.compile(r"(\d{2})/([A-Z][a-z]{2})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})")
_GIT = re.compile(r"[A-Z][a-z]{2} ([A-Z][a-z]{2}) +(\d{1,2}) (\d{2}):(\d{2}):(\d{2}) (\d{4}) "
                  r"([+-]\d{4})")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?")
_ZONES: dict[str, ZoneInfo] = {}


def _zone(name: str | None):
    if name is None or name == "UTC":
        return UTC
    z = _ZONES.get(name)
    if z is None:
        z = _ZONES[name] = ZoneInfo(name)
    return z


def _us(dt: datetime) -> int:
    return (dt - _EPOCH) // _US


def _offset(s: str) -> timedelta:
    sign = -1 if s[0] == "-" else 1
    return sign * timedelta(hours=int(s[1:3]), minutes=int(s[3:5]))


def is_naive(ts_raw: str) -> bool:
    """True for spellings that carry no zone (log4j, ISO without offset)."""
    if _LOG4J.fullmatch(ts_raw):
        return True
    m = _ISO.fullmatch(ts_raw)
    return bool(m and not m.group(3))


def to_epoch_us(ts_raw: str, host_tz_hint: str | None = None) -> int:
    """Epoch microseconds for any supported spelling; ValueError otherwise."""
    s = ts_raw.strip()
    if _DIGITS.fullmatch(s):
        n, digits = int(s), len(s.lstrip("-"))
        return (n * 1_000_000 if digits <= 11 else n * 1000 if digits <= 14
                else n if digits <= 17 else n // 1000)
    if m := _SLACK.fullmatch(s):
        return int(m[1]) * 1_000_000 + int(m[2].ljust(6, "0")[:6])
    if m := _LOG4J.fullmatch(s):
        y, mo, d, h, mi, se, frac = m.groups()
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(se),
                      int(frac.ljust(6, "0")), tzinfo=_zone(host_tz_hint))
        return _us(dt)
    if m := _NGINX.fullmatch(s):
        d, mon, y, h, mi, se, off = m.groups()
        dt = datetime(int(y), _MON[mon], int(d), int(h), int(mi), int(se), tzinfo=UTC)
        return _us(dt - _offset(off))
    if m := _GIT.fullmatch(s):
        mon, d, h, mi, se, y, off = m.groups()
        dt = datetime(int(y), _MON[mon], int(d), int(h), int(mi), int(se), tzinfo=UTC)
        return _us(dt - _offset(off))
    if _ISO.fullmatch(s):
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_zone(host_tz_hint))
        return _us(dt)
    raise ValueError(f"unrecognised timestamp {ts_raw!r}")


def normalize_ts(ts_raw: str, host_tz_hint: str | None) -> datetime:
    return _EPOCH + timedelta(microseconds=to_epoch_us(ts_raw, host_tz_hint))


def infer_tz(naive_ts_raw: str, reference_us: int,
             candidates: tuple[str, ...] = CANDIDATE_TZS) -> str:
    """The zone in which `naive_ts_raw` (a file's median stamp) lands nearest
    to `reference_us` (the median of the scenario's zone-aware stamps)."""
    return min(candidates, key=lambda z: abs(to_epoch_us(naive_ts_raw, z) - reference_us))


# ---- clock skew ----

_EDGE_SCHEMA = {"a": pl.String, "b": pl.String, "d": pl.Float64}


def _solve(edges: pl.DataFrame) -> dict[str, int]:
    """Per-host skew in ms from edges (a, b, d = t_b - t_a in ms). Hosts with
    fewer than MIN_PAIRS edges are left out (no evidence either way)."""
    if edges.is_empty():
        return {}
    s = pl.DataFrame({"host": pl.concat([edges["a"], edges["b"]]).unique()}).with_columns(
        s=pl.lit(0.0), n=pl.lit(0, dtype=pl.UInt32))
    for _ in range(_ITERATIONS):
        e = (edges.join(s.select(a="host", sa="s"), on="a", how="left")
             .join(s.select(b="host", sb="s"), on="b", how="left"))
        lat = e.select((pl.col("d") - pl.col("sb") + pl.col("sa")).median()).item()
        est = pl.concat([
            e.select(host="b", est=pl.col("d") - lat + pl.col("sa")),
            e.select(host="a", est=pl.col("sb") - pl.col("d") + lat),
        ])
        s = est.group_by("host").agg(s=pl.col("est").median(), n=pl.len())
        s = s.with_columns(pl.col("s") - pl.col("s").median())
    return {host: round(skew) if abs(skew) >= SKEW_MIN_MS else 0
            for host, skew, n in s.iter_rows() if n >= MIN_PAIRS}


def _ms(col: str) -> pl.Expr:
    return pl.col(col).cast(pl.Datetime("us", "UTC")).dt.epoch("us").cast(pl.Float64) / 1000.0


def log_pair_edges(logs_df: pl.DataFrame) -> pl.DataFrame:
    df = logs_df.filter(pl.col("request_id").is_not_null()).select(
        "host", "request_id", "msg", t=_ms("ts_utc"))
    caller = df.filter(pl.col("msg").str.starts_with("Calling ")).select(
        "request_id", a="host", ta="t")
    callee = df.filter(pl.col("msg").str.starts_with("Received ")).select(
        "request_id", b="host", tb="t")
    pairs = caller.join(callee, on="request_id").filter(pl.col("a") != pl.col("b"))
    return pairs.select("a", "b", d=pl.col("tb") - pl.col("ta")).cast(_EDGE_SCHEMA)


def span_edges(spans_df: pl.DataFrame) -> pl.DataFrame:
    df = spans_df.filter(pl.col("host").is_not_null()).select(
        "trace_id", "span_id", "parent_span_id", "host", t=_ms("ts_utc"))
    parent = df.select("trace_id", parent_span_id="span_id", a="host", ta="t")
    child = df.filter(pl.col("parent_span_id").is_not_null()).select(
        "trace_id", "parent_span_id", b="host", tb="t")
    pairs = child.join(parent, on=["trace_id", "parent_span_id"]).filter(pl.col("a") != pl.col("b"))
    return pairs.select("a", "b", d=pl.col("tb") - pl.col("ta")).cast(_EDGE_SCHEMA)


def span_skew(spans_df: pl.DataFrame) -> dict[str, int]:
    """Tracer-clock skew per host from cross-host parent/child spans."""
    return _solve(span_edges(spans_df))


def estimate_skew(spans_df: pl.DataFrame, logs_df: pl.DataFrame) -> dict[str, int]:
    """Log-clock skew (ms) for every host in `logs_df`: from request-id pairs
    when the host has them, else from its tracer clock, else 0."""
    from_logs = _solve(log_pair_edges(logs_df))
    from_spans = span_skew(spans_df)
    hosts = logs_df.select(pl.col("host").drop_nulls().unique()).to_series().to_list()
    return {h: from_logs[h] if h in from_logs else from_spans.get(h, 0) for h in sorted(hosts)}
