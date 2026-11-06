"""Trace enricher: one trc:TraceEdge per caller>callee key, one
ops:TraceEdgeFingerprint per window where the edge got slower or failed more
than its own trailing baseline.

Input is `derived/fp_edges` (Task 8): per edge key and minute, span count,
p50/p99 duration and error rate. Its rows partition the spans table, so:
  * TraceEdge: every key, with ops:count (all its spans), trc:errorRate
    (pooled), trc:p50Ms (median of the per-minute medians), window bounds and
    ops:rawSelector (plus the no-time sentinel selector when it has untimed
    spans). Together the edges select every span.
  * TraceEdgeFingerprint, signal type slow_trace (per-minute p50 at least
    2x its trailing-hour median and 6 robust sigmas over it) or failed_trace
    (per-minute error rate 6 binomial sigmas over the trailing-hour pooled rate,
    with at least 3 errors). The onset threshold holds for the whole run, which
    ends after more than 2 minutes back under it. Minutes with fewer than 3
    spans never start or extend a run. ops:magnitude is the peak over the
    baseline; trc:p50Ms / trc:p99Ms are the peak per-minute values; the
    selector covers exactly the window's minutes, so ops:count = its spans.
e2e_bottleneck (the critical-path edge of one request) needs per-trace
analysis and is not emitted."""
from datetime import timedelta

import numpy as np
import polars as pl
from rdflib import URIRef
from rdflib.namespace import RDF

from e2ekg.graph.enricher import NS, Enricher, dec, finite, integer, node, read_table, string, ts
from e2ekg.graph.uris import concept
from e2ekg.preprocess import selector as sel

OPS, TRC = NS["ops"], NS["trc"]
MINUTE = timedelta(minutes=1)
BASE_MIN, MIN_BASE_POINTS, MIN_SPANS, MISS_MAX = 60, 10, 3, 2
SLOW_RATIO, Z, MIN_ERRORS, P_FLOOR = 2.0, 6.0, 3, 0.005


def runs(x: np.ndarray, ok: np.ndarray, threshold) -> list[tuple[int, int, float]]:
    """(first, last, baseline) index runs. Onset at i when ok[i] and
    x[i] >= threshold(i) (threshold returns (value, baseline) or None when there
    is no baseline yet); the onset threshold holds while the run lasts."""
    out, n, i = [], len(x), 0
    while i < n:
        t = threshold(i) if ok[i] else None
        if t is None or x[i] < t[0]:
            i += 1
            continue
        j, m, miss = i, i + 1, 0
        while m < n and miss <= MISS_MAX:
            if ok[m] and x[m] >= t[0]:
                j, miss = m, 0
            else:
                miss += 1
            m += 1
        out.append((i, j, t[1]))
        i = j + 1
    return out


def slow_windows(p50: np.ndarray, n: np.ndarray) -> list[tuple[int, int, float]]:
    ok = n >= MIN_SPANS

    def threshold(i):
        lo = max(0, i - BASE_MIN)
        b = p50[lo:i][ok[lo:i]]
        if len(b) < MIN_BASE_POINTS:
            return None
        med = float(np.median(b))
        sigma = max(1.4826 * float(np.median(np.abs(b - med))), 0.05 * med, 1e-6)
        return max(SLOW_RATIO * med, med + Z * sigma), med
    return runs(p50, ok, threshold)


def failed_windows(rate: np.ndarray, n: np.ndarray) -> list[tuple[int, int, float]]:
    ok = (n >= MIN_SPANS) & (rate * n >= MIN_ERRORS - 1e-9)

    def threshold(i):
        lo = max(0, i - BASE_MIN)
        nb = n[lo:i]
        if (nb >= MIN_SPANS).sum() < MIN_BASE_POINTS:
            return None
        p0 = max(float((rate[lo:i] * nb).sum() / nb.sum()), P_FLOOR)
        return p0 + Z * float(np.sqrt(p0 * (1 - p0) / n[i])), p0
    return runs(rate, ok, threshold)


def _app(u: str | None) -> URIRef | None:
    return URIRef(u) if u else None


class TraceFingerprintEnricher(Enricher):
    name = "traces"
    reads = ("derived/fp_edges",)

    def fetch(self, ctx):
        return read_table(ctx, "derived/fp_edges").sort("key", "minute", nulls_last=True)

    def analyze(self, fp: pl.DataFrame):
        edges, windows = [], []
        for (key,), g in fp.group_by(["key"], maintain_order=True):
            caller, callee = g["caller_uri"][0], g["callee_uri"][0]
            timed = g.filter(pl.col("minute").is_not_null())
            k = (g["n"] * g["error_rate"]).sum()
            edges.append({"key": key, "caller": caller, "callee": callee, "n": int(g["n"].sum()),
                          "errors": float(k), "untimed": g.height - timed.height,
                          "p50": float(timed["p50_ms"].median()) if timed.height else None,
                          "t0": timed["minute"].min(), "t1": timed["minute"].max()})
            if timed.height == 0:
                continue
            t0 = timed["minute"].min()
            idx = ((timed["minute"] - t0) / MINUTE).cast(pl.Int64).to_numpy()
            size = int(idx.max()) + 1
            cols = {}
            for c in ("n", "p50_ms", "p99_ms", "error_rate"):
                a = np.zeros(size)
                a[idx] = timed[c].cast(pl.Float64).fill_null(0.0).to_numpy()
                cols[c] = a
            for kind, found in (("slow_trace", slow_windows(cols["p50_ms"], cols["n"])),
                                ("failed_trace", failed_windows(cols["error_rate"], cols["n"]))):
                for i, j, base in found:
                    span = slice(i, j + 1)
                    nn = cols["n"][span]
                    peak = (cols["p50_ms"] if kind == "slow_trace" else cols["error_rate"])[span]
                    windows.append({
                        "key": key, "caller": caller, "callee": callee, "kind": kind,
                        "start": t0 + i * MINUTE, "end": t0 + (j + 1) * MINUTE,
                        "n": int(nn.sum()), "magnitude": float(peak.max()) / base,
                        "p50": float(cols["p50_ms"][span].max()),
                        "p99": float(cols["p99_ms"][span].max()),
                        "rate": float((cols["error_rate"][span] * nn).sum() / nn.sum())})
        return edges, windows

    def emit(self, result):
        edges, windows = result
        sc = self.scenario
        for e in edges:
            u = node("trc", "edge", sc, e["key"])
            yield u, RDF.type, TRC.TraceEdge
            yield from _ends(u, e["caller"], e["callee"])
            yield u, OPS["count"], integer(e["n"])
            yield u, TRC.errorRate, dec(round(e["errors"] / e["n"], 6))
            if e["p50"] is not None and finite(e["p50"]):
                yield u, TRC.p50Ms, dec(round(e["p50"], 3))
            if e["t0"] is not None:
                end = e["t1"] + MINUTE
                yield u, OPS.windowStart, ts(e["t0"])
                yield u, OPS.windowEnd, ts(end)
                yield u, OPS.rawSelector, string(sel.make("spans", e["callee"], e["key"],
                                                          e["t0"], end))
            if e["untimed"]:
                yield u, OPS.rawSelector, string(sel.make("spans", e["callee"], e["key"],
                                                          None, None))
        for w in windows:
            u = node("trc", "fingerprint", sc, w["key"], w["kind"], w["start"])
            yield u, RDF.type, OPS.TraceEdgeFingerprint
            yield u, OPS.signalType, concept("signal_type", w["kind"])
            yield u, TRC.onEdge, node("trc", "edge", sc, w["key"])
            yield from _ends(u, w["caller"], w["callee"])
            yield u, OPS.atTime, ts(w["start"])
            yield u, OPS.windowStart, ts(w["start"])
            yield u, OPS.windowEnd, ts(w["end"])
            yield u, OPS["count"], integer(w["n"])
            if finite(w["magnitude"]):
                yield u, OPS.magnitude, dec(round(w["magnitude"], 6))
            yield u, TRC.p50Ms, dec(round(w["p50"], 3))
            yield u, TRC.p99Ms, dec(round(w["p99"], 3))
            yield u, TRC.errorRate, dec(round(w["rate"], 6))
            yield u, OPS.rawSelector, string(sel.make("spans", w["callee"], w["key"],
                                                      w["start"], w["end"]))


def _ends(u, caller, callee):
    if callee:
        yield u, OPS.onApplication, _app(callee)
    if caller:
        yield u, TRC.caller, _app(caller)
