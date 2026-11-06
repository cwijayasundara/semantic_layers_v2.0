"""trc.* injectors over OTLP ResourceSpans records (fields: resource, scope,
spans[]) and Zipkin v2 records (one span per record).

Renderers emit every span. trc.sampling then decides which traces were ever
collected (head sampling plus keep-all-errors), and drops the rest. That
drop is not a lossless-contract violation: an unsampled span never reaches
a real collector, so it never existed in the raw layer. The other trace
injectors only touch traces that sampling keeps, so their corruption
survives into the raw files."""
import hashlib
from dataclasses import replace
from datetime import timedelta

from e2ekg.generate.mess._base import injector, pick, resolver, shift_ts, ts_utc

HEAD_SAMPLE_RATE = 0.01
ORPHAN_RATE, SKEW_RATE = 0.01, 0.01
LEGACY_ZIPKIN_APPS = frozenset({"ratings-svc", "artwork-metadata-svc"})
_ZIPKIN_KIND = {2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}


def head_sampled(trace_id: str) -> bool:
    """TraceIdRatio-style: a pure function of the trace id, like real samplers."""
    h = int(hashlib.sha256(trace_id.encode()).hexdigest()[:8], 16)
    return h % 10_000 < HEAD_SAMPLE_RATE * 10_000


def _spans(self, recs):
    """(rec_idx, span_idx|None, trace_id, span_id, parent_id, is_error)."""
    for i, r in enumerate(recs):
        if self.eligible(r, {"otlp_jsonl"}):
            for j, s in enumerate(r.fields["spans"]):
                yield (i, j, s["traceId"], s["spanId"], s.get("parentSpanId") or "",
                       s.get("status", {}).get("code") == 2)
        elif self.eligible(r, {"zipkin_jsonl"}):
            f = r.fields
            yield (i, None, f["traceId"], f["id"], f.get("parentId") or "",
                   "error" in f.get("tags", {}))


def _kept(rows) -> set[str]:
    return {r[2] for r in rows if r[5] or head_sampled(r[2])}


def _set_span(recs, i, j, **upd):
    spans = list(recs[i].fields["spans"])
    spans[j] = {**spans[j], **upd}
    recs[i] = replace(recs[i], fields={**recs[i].fields, "spans": spans})


def _new_trace_id(rng, taken: set[str]) -> str:
    while True:
        tid = "".join(f"{int(b):02x}" for b in rng.integers(0, 256, size=16))
        if head_sampled(tid) and tid not in taken:
            return tid


@injector("trc.orphan", ("traces",))
def orphan(self, recs, rng, scenario):
    """Broken context propagation: a parent span is exported under a fresh
    trace id, so its children's parentSpanId points at nothing in their trace.
    The parent span itself is kept; the original trace id is in the detail."""
    rows = list(_spans(self, recs))
    kept = _kept(rows)
    present = {(r[2], r[3]): (r[0], r[1]) for r in rows if r[1] is not None}
    parents: dict[str, dict[str, list[str]]] = {}
    for _, j, tid, sid, par, _ in rows:
        if tid in kept and (tid, par) in present:
            parents.setdefault(tid, {}).setdefault(par, []).append(sid)
    tids = list(parents)
    out, inj = list(recs), []
    for k in pick(rng, len(tids), ORPHAN_RATE):
        tid = tids[k]
        psid = list(parents[tid])[int(rng.integers(len(parents[tid])))]
        new = _new_trace_id(rng, {r[2] for r in rows})
        i, j = present[(tid, psid)]
        _set_span(out, i, j, traceId=new)
        inj.append(self.record(recs[i].rel_path, trace_id=tid, span_id=psid, new_trace_id=new,
                               orphaned_children=parents[tid][psid]))
    return out, inj


@injector("trc.skew", ("traces",))
def skew(self, recs, rng, scenario):
    """Host clock skew: a child span starts before its parent."""
    rows = list(_spans(self, recs))
    kept = _kept(rows)
    present = {(r[2], r[3]): (r[0], r[1]) for r in rows if r[1] is not None}
    cands = [r for r in rows if r[1] is not None and r[2] in kept and (r[2], r[4]) in present]
    out, inj = list(recs), []
    for k in pick(rng, len(cands), SKEW_RATE):
        i, j, tid, sid, par, _ = cands[k]
        pi, pj = present[(tid, par)]
        child = out[i].fields["spans"][j]
        p_start = ts_utc(out[pi].fields["spans"][pj]["start"])
        lead_ms = int(rng.integers(50, 301))
        delta = (ts_utc(child["start"]) - p_start) + timedelta(milliseconds=lead_ms)
        _set_span(out, i, j, start=shift_ts(child["start"], -delta),
                  end=shift_ts(child["end"], -delta))
        inj.append(self.record(recs[i].rel_path, trace_id=tid, span_id=sid, parent_span_id=par,
                               shift_us=-(delta // timedelta(microseconds=1)),
                               lead_before_parent_ms=lead_ms))
    return out, inj


def _tag(v) -> str:
    return ("true" if v else "false") if isinstance(v, bool) else str(v)


def _to_zipkin(r) -> list:
    res = r.fields["resource"]
    svc = res["service.name"]
    rel = r.rel_path.replace("traces/otlp/", "traces/zipkin/", 1)
    out = []
    for s in r.fields["spans"]:
        tags = {k: _tag(v) for k, v in s.get("attributes", {}).items()}
        tags |= {k: _tag(v) for k, v in res.items() if k != "service.name"}
        if s.get("status", {}).get("code") == 2:
            tags["error"] = "true"
        z = {"traceId": s["traceId"], "id": s["spanId"]}
        if s.get("parentSpanId"):
            z["parentId"] = s["parentSpanId"]
        z["name"] = s["name"].lower()
        if s.get("kind") in _ZIPKIN_KIND:
            z["kind"] = _ZIPKIN_KIND[s["kind"]]
        z |= {"timestamp": s["start"],
              "duration": (ts_utc(s["end"]) - ts_utc(s["start"])) // timedelta(microseconds=1),
              "localEndpoint": {"serviceName": svc.lower()}, "tags": tags}
        out.append(replace(r, rel_path=rel, fmt="zipkin_jsonl", fields=z,
                           at_utc=ts_utc(s["start"])))
    return out


@injector("trc.zipkin_legacy", ("traces",))
def zipkin_legacy(self, recs, rng, scenario):
    """Renderers emit OTLP for every app; two legacy apps really export Zipkin
    v2, one span per line, lower-cased names, string tags."""
    resolve = resolver(scenario)
    out, inj = [], []
    for r in recs:
        if (self.eligible(r, {"otlp_jsonl"})
                and resolve(r.fields["resource"]["service.name"]) in LEGACY_ZIPKIN_APPS):
            z = _to_zipkin(r)
            out += z
            inj.append(self.record(z[0].rel_path if z else r.rel_path, from_rel_path=r.rel_path,
                                   service=r.fields["resource"]["service.name"], n_spans=len(z)))
        else:
            out.append(r)
    return out, inj


@injector("trc.sampling", ("traces",))
def sampling(self, recs, rng, scenario):
    """Head sampling (HEAD_SAMPLE_RATE, by trace id) plus keep-all-errors."""
    rows = list(_spans(self, recs))
    errors = {r[2] for r in rows if r[5]}
    keep = _kept(rows)
    stats: dict[str, dict] = {}
    for i, _, tid, *_ in rows:
        st = stats.setdefault(recs[i].rel_path, {"tids": set(), "dropped_spans": 0})
        st["tids"].add(tid)
        st["dropped_spans"] += tid not in keep
    out = []
    for r in recs:
        if self.eligible(r, {"otlp_jsonl"}):
            spans = [s for s in r.fields["spans"] if s["traceId"] in keep]
            if spans:
                out.append(r if len(spans) == len(r.fields["spans"])
                           else replace(r, fields={**r.fields, "spans": spans}))
        elif not self.eligible(r, {"zipkin_jsonl"}) or r.fields["traceId"] in keep:
            out.append(r)
    inj = []
    for p, st in stats.items():
        t = st["tids"]
        head = {x for x in t if head_sampled(x)}
        inj.append(self.record(p, traces_seen=len(t), kept_head=len(head),
                               kept_error=len((t & errors) - head), dropped_traces=len(t - keep),
                               dropped_spans=st["dropped_spans"], head_rate=HEAD_SAMPLE_RATE))
    return out, inj


INJECTORS = (orphan, skew, zipkin_legacy, sampling)
