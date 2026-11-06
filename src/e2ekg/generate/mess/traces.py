"""trc.* injectors over OTLP ResourceSpans records (fields: resource, scope,
spans[]) and Zipkin v2 records (one span per record).

Head sampling is the trace renderer's job (Ruling R14): at the root, before
any span is materialized, it calls `head_sampled(trace_id, has_error)` and
renders only kept traces. Every trace these injectors see was therefore
collected. trc.sampling models a real defect on top of that: two legacy
services ignore the propagated sampled flag and export nothing for ~30% of
kept traces. Those spans never reached a collector, so dropping them is not
a lossless-contract violation; each drop is recorded per trace. No trace
injector ever drops a span from a record tagged `evidence:*`."""
import hashlib
import json
from dataclasses import replace
from datetime import timedelta

from e2ekg.generate.mess._base import injector, pick, resolver, shift_ts, ts_utc

HEAD_SAMPLE_RATE = 0.01
ORPHAN_RATE, SKEW_RATE, PROPAGATION_DROP_RATE = 0.01, 0.01, 0.3
LEGACY_ZIPKIN_APPS = frozenset({"ratings-svc", "artwork-metadata-svc"})
# Services whose old tracing client ignores the parent's sampled flag.
SAMPLING_IGNORANT_APPS = LEGACY_ZIPKIN_APPS
_ZIPKIN_KIND = {2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
_OTEL_STATUS = {1: "OK", 2: "ERROR"}


def head_sampled(trace_id: str, has_error: bool = False) -> bool:
    """The head-sampling decision the trace renderer makes at the root (R14).

    TraceIdRatio-style: a pure function of the trace id (HEAD_SAMPLE_RATE of
    ids pass), so it is seeded by however the renderer draws trace ids and
    consumes no rng itself. `has_error=True` always keeps the trace
    (keep-all-errors); the renderer knows this from the scenario before it
    materializes spans."""
    if has_error:
        return True
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


def _set_span(recs, i, j, **upd):
    spans = list(recs[i].fields["spans"])
    spans[j] = {**spans[j], **upd}
    recs[i] = replace(recs[i], fields={**recs[i].fields, "spans": spans})


def _new_trace_id(rng, taken: set[str]) -> str:
    while True:
        tid = "".join(f"{int(b):02x}" for b in rng.integers(0, 256, size=16))
        if tid not in taken:
            return tid


@injector("trc.orphan", ("traces",))
def orphan(self, recs, rng, scenario):
    """Broken context propagation: a parent span is exported under a fresh
    trace id, so its children's parentSpanId points at nothing in their trace.
    The parent span itself is kept; the original trace id is in the detail."""
    rows = list(_spans(self, recs))
    present = {(r[2], r[3]): (r[0], r[1]) for r in rows if r[1] is not None}
    parents: dict[str, dict[str, list[str]]] = {}
    for _, _, tid, sid, par, _ in rows:
        if (tid, par) in present:
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
    present = {(r[2], r[3]): (r[0], r[1]) for r in rows if r[1] is not None}
    cands = [r for r in rows if r[1] is not None and (r[2], r[4]) in present]
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
    """Zipkin tags are strings: bools lower-case, lists/dicts JSON, else str()."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple, dict)):
        return json.dumps(_plain(v), ensure_ascii=False, separators=(",", ":"))
    if ts_utc(v) is not None:
        return ts_utc(v).isoformat()
    return str(v)


def _plain(v):
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return ts_utc(v).isoformat() if ts_utc(v) is not None else v


_SPAN_MAPPED = {"traceId", "spanId", "parentSpanId", "name", "kind", "start", "end",
                "attributes", "status", "events"}


def _to_zipkin(r) -> tuple[list, list[str]]:
    """Lossless OTLP -> Zipkin v2. Names and serviceName keep their surface
    form exactly. Span attributes win; a colliding resource attribute is kept
    as tag `otel.resource.<key>` (returned for the detail). Status becomes
    `otel.status_code` plus `error` (= status message) or
    `otel.status_description`; events become annotations; the scope is the
    `otel.scope.name` tag; any other span key becomes an `otel.<key>` JSON tag."""
    res = r.fields["resource"]
    rel = r.rel_path.replace("traces/otlp/", "traces/zipkin/", 1)
    out, collisions = [], set()
    for s in r.fields["spans"]:
        tags = {k: _tag(v) for k, v in s.get("attributes", {}).items()}
        for k, v in res.items():
            if k == "service.name":
                continue
            if k in tags:
                collisions.add(k)
                tags[f"otel.resource.{k}"] = _tag(v)
            else:
                tags[k] = _tag(v)
        if r.fields.get("scope") is not None:
            tags["otel.scope.name"] = str(r.fields["scope"])
        st = s.get("status") or {}
        if st.get("code"):
            tags["otel.status_code"] = _OTEL_STATUS.get(st["code"], str(st["code"]))
        if st.get("code") == 2:
            tags["error"] = st.get("message") or "true"
        elif st.get("message"):
            tags["otel.status_description"] = st["message"]
        if s.get("kind") not in _ZIPKIN_KIND and s.get("kind") is not None:
            tags["otel.span_kind"] = str(s["kind"])
        for k, v in s.items():
            if k not in _SPAN_MAPPED:
                tags[f"otel.{k}"] = _tag(v)
        z = {"traceId": s["traceId"], "id": s["spanId"]}
        if s.get("parentSpanId"):
            z["parentId"] = s["parentSpanId"]
        z["name"] = s["name"]
        if s.get("kind") in _ZIPKIN_KIND:
            z["kind"] = _ZIPKIN_KIND[s["kind"]]
        z |= {"timestamp": s["start"],
              "duration": (ts_utc(s["end"]) - ts_utc(s["start"])) // timedelta(microseconds=1),
              "localEndpoint": {"serviceName": res["service.name"]}}
        if s.get("events"):
            z["annotations"] = [
                {"timestamp": e["time"],
                 "value": e["name"] + (f" {_tag(e['attributes'])}" if e.get("attributes") else "")}
                for e in s["events"]]
        z["tags"] = tags
        out.append(replace(r, rel_path=rel, fmt="zipkin_jsonl", fields=z,
                           at_utc=ts_utc(s["start"])))
    return out, sorted(collisions)


@injector("trc.zipkin_legacy", ("traces",))
def zipkin_legacy(self, recs, rng, scenario):
    """Renderers emit OTLP for every app; two legacy apps really export Zipkin
    v2, one span per line, string tags."""
    resolve = resolver(scenario)
    out, inj = [], []
    for r in recs:
        if (self.eligible(r, {"otlp_jsonl"})
                and resolve(r.fields["resource"]["service.name"]) in LEGACY_ZIPKIN_APPS):
            z, collisions = _to_zipkin(r)
            out += z
            inj.append(self.record(z[0].rel_path if z else r.rel_path, from_rel_path=r.rel_path,
                                   service=r.fields["resource"]["service.name"], n_spans=len(z),
                                   prefixed_resource_keys=collisions))
        else:
            out.append(r)
    return out, inj


def _is_evidence(r) -> bool:
    return any(t.startswith("evidence:") for t in r.tags)


@injector("trc.sampling", ("traces",))
def sampling(self, recs, rng, scenario):
    """Inconsistent sampling propagation: SAMPLING_IGNORANT_APPS export no
    spans for ~PROPAGATION_DROP_RATE of the (already head-sampled) traces.
    Spans in `evidence:*`-tagged records are never dropped."""
    resolve = resolver(scenario)

    def app_of(r):
        f = r.fields
        name = (f["resource"]["service.name"] if r.fmt == "otlp_jsonl"
                else f.get("localEndpoint", {}).get("serviceName", ""))
        return resolve(name)

    droppable: dict[str, list[tuple[int, int | None, str]]] = {}
    for i, j, tid, sid, _, _ in _spans(self, recs):
        if app_of(recs[i]) in SAMPLING_IGNORANT_APPS and not _is_evidence(recs[i]):
            droppable.setdefault(tid, []).append((i, j, sid))
    tids = list(droppable)
    drop_span: dict[int, set[int]] = {}   # otlp rec idx -> span idxs
    drop_rec: set[int] = set()            # zipkin rec idx
    inj = []
    for k in pick(rng, len(tids), PROPAGATION_DROP_RATE):
        tid = tids[k]
        per_path: dict[str, list[str]] = {}
        for i, j, sid in droppable[tid]:
            if j is None:
                drop_rec.add(i)
            else:
                drop_span.setdefault(i, set()).add(j)
            per_path.setdefault(recs[i].rel_path, []).append(sid)
        for p, sids in per_path.items():
            inj.append(self.record(p, trace_id=tid, dropped_span_ids=sids,
                                   apps=sorted({app_of(recs[i]) for i, _, _ in droppable[tid]}),
                                   reason="sampled flag ignored"))
    out = []
    for i, r in enumerate(recs):
        if i in drop_rec:
            continue
        if i in drop_span:
            spans = [s for j, s in enumerate(r.fields["spans"]) if j not in drop_span[i]]
            if not spans:
                continue
            r = replace(r, fields={**r.fields, "spans": spans})
        out.append(r)
    return out, inj


INJECTORS = (orphan, skew, zipkin_legacy, sampling)
