"""traces/otlp/<HHMM>.jsonl: OTLP/JSON ResourceSpans, one per line (R13).

The conceptual traffic is a diurnal Poisson stream of requests per route
(home, play, search) entering at the edge. Head sampling happens here, at the
root, before any span exists (R14/R16): a trace is materialized only if
`head_sampled(trace_id, has_error)` keeps it. Kept spans are batched per
(minute file, app, host) like an exporter would. Evidence slow traces (one
per 5-minute bucket after the earliest slow_trace link, on a route through
every slow_trace app) are head-sampled traces in their own records, tagged
`evidence:slow_trace`. Zipkin is never rendered here: the trc.zipkin_legacy
injector converts the two legacy apps. A route is scaled by `scale` unless
it passes through the fault target."""
from dataclasses import dataclass, field

import numpy as np

from e2ekg.generate.mess.traces import head_sampled
from e2ekg.generate.records import RawRecord
from e2ekg.generate.world import BASE_ERROR, World, epoch, recovery_rng, tags, utc
from e2ekg.scenario.model import Scenario
from e2ekg.scenario.propagation import Degradation

ZIPF_S = 1.3            # (shared realism constant; unused by traces)
DIURNAL_AMP = 0.35
TEMPLATES_PER_APP = (20, 40)  # (shared realism constant; unused by traces)

# Conceptual requests over the 6 h window at scale 1.0.
ROUTE_TRACES = {"home": 600_000, "play": 250_000, "search": 150_000}
ROUTE_ENTRY = {"home": "homepage-graphql", "play": "playback-api", "search": "search-svc"}
EDGE = "edge-gateway"
BATCH_SPANS = 50
EVIDENCE_BUCKET_S = 300
CACHE_MISS = 0.15
SCOPE = "io.opentelemetry.grpc-1.6"
_KIND = {"server": 2, "client": 3}


@dataclass
class Node:
    app: str
    children: list["Node"] = field(default_factory=list)
    client_of: str | None = None   # datastore/cache: a CLIENT span in this caller
    optional: bool = False         # cassandra: only on a cache miss


def _tree(w: World, app: str, route: str, depth: int = 0) -> Node:
    node = Node(app)
    callees = w.callees.get(app, ())
    has_cache = any(w.apps[e.callee].kind == "cache" for e in callees)
    for e in callees:
        callee = w.apps[e.callee]
        if callee.kind in ("datastore", "cache"):
            node.children.append(Node(e.callee, client_of=app,
                                      optional=callee.kind == "datastore" and has_cache))
        elif callee.aliases or (route == "search" and depth < 2):
            if callee.kind != "client":
                node.children.append(_tree(w, e.callee, route, depth + 1))
    return node


def _apps(n: Node) -> set[str]:
    out = {n.app}
    for c in n.children:
        out |= _apps(c)
    return out


@dataclass
class Population:
    traces_considered: int = 0
    traces_kept_head: int = 0
    traces_kept_error: int = 0
    traces_shed: int = 0
    spans_kept: int = 0
    evidence_traces: int = 0
    by_route: dict = field(default_factory=dict)

    @property
    def traces_kept(self) -> int:
        return self.traces_kept_head + self.traces_kept_error

    def as_dict(self) -> dict:
        return {"traces_considered": self.traces_considered,
                "traces_kept_head": self.traces_kept_head,
                "traces_kept_error": self.traces_kept_error, "traces_kept": self.traces_kept,
                "traces_shed": self.traces_shed, "spans_kept": self.spans_kept,
                "evidence_traces": self.evidence_traces, "head_sample_rate": 0.01,
                "by_route": self.by_route}


class _Builder:
    def __init__(self, w: World, rng) -> None:
        self.w, self.rng = w, rng
        self.own = {}
        self.hosts = {}
        self._ids = iter(())

    def own_extra(self, app: str, t: float) -> float:
        if app not in self.own:
            self.own[app] = self.w.own_extra_grid(app)
        return float(self.own[app][int(self.w.grid_index(t))])

    def host(self, app: str) -> str:
        hs = self.hosts.setdefault(app, self.w.hosts(app))
        return hs[int(self.rng.integers(len(hs)))]

    def span_id(self) -> str:
        sid = next(self._ids, None)
        if sid is None:
            raw = self.rng.bytes(8 * 4096).hex()
            self._ids = iter([raw[i:i + 16] for i in range(0, len(raw), 16)])
            sid = next(self._ids)
        return sid

    def build(self, tid: str, root: Node, t: float, fail_app: str | None, fail_msg: str,
              miss_p: float) -> list[tuple[str, str, dict]]:
        """Spans as (app, host, span) with parents before children."""
        out: list[tuple[str, str, dict]] = []

        def rec(n: Node, start: float, parent_id: str,
                parent_host: str | None) -> tuple[float, bool]:
            w = self.w
            p50 = w.base(n.app)[0]
            if n.client_of is not None:
                dur = (p50 * float(np.exp(0.5 * self.rng.standard_normal()))
                       + self.own_extra(n.app, t))
                db = "memcached" if w.apps[n.app].kind == "cache" else "cassandra"
                err = n.app == fail_app
                span = {"traceId": tid, "spanId": self.span_id(), "parentSpanId": parent_id,
                        "name": "get" if db == "memcached" else "SELECT titles.by_id",
                        "kind": _KIND["client"], "start": utc(start),
                        "end": utc(start + dur / 1000),
                        "attributes": {"db.system": db, "peer.service": n.app,
                                       "db.operation": "get" if db == "memcached" else "SELECT"},
                        "status": {"code": 2, "message": fail_msg} if err else {"code": 0}}
                out.append((n.client_of, parent_host, span))
                return dur, err
            self_ms = 0.6 * p50 * float(np.exp(0.4 * self.rng.standard_normal()))
            self_ms += self.own_extra(n.app, t)
            sid = self.span_id()
            host = self.host(n.app)
            idx = len(out)
            out.append((n.app, host, {}))
            pre = 0.3 * self_ms
            longest, child_err = 0.0, False
            for c in n.children:
                if c.optional and c.app != fail_app and self.rng.random() >= miss_p:
                    continue
                d, e = rec(c, start + (pre + float(self.rng.random())) / 1000, sid, host)
                longest = max(longest, d + 1.0)
                child_err |= e
            dur = pre + longest + 0.7 * self_ms
            err = child_err or n.app == fail_app
            span = {"traceId": tid, "spanId": sid, "parentSpanId": parent_id,
                    "name": _span_name(n.app), "kind": _KIND["server"], "start": utc(start),
                    "end": utc(start + dur / 1000), "attributes": _attrs(w, n.app, err),
                    "status": ({"code": 2, "message": fail_msg if n.app == fail_app
                                else "upstream error"} if err else {"code": 0})}
            out[idx] = (n.app, host, span)
            return dur, err

        rec(root, t, "", None)
        return out


def _span_name(app: str) -> str:
    if app == EDGE:
        return "POST /graphql"
    if app == "homepage-graphql":
        return "query HomePage"
    base = "".join(p.capitalize() for p in app.split("-") if p not in ("svc", "api"))
    return f"netflix.{app.replace('-', '')}.{base}Service/Get{base}"


def _attrs(w: World, app: str, err: bool) -> dict:
    if w.apps[app].kind == "edge" or app == "homepage-graphql":
        return {"http.method": "POST", "http.route": "/graphql",
                "http.status_code": 504 if err else 200}
    base = "".join(p.capitalize() for p in app.split("-") if p not in ("svc", "api"))
    return {"rpc.system": "grpc", "rpc.service": f"netflix.{app.replace('-', '')}.{base}Service",
            "rpc.method": f"Get{base}", "rpc.grpc.status_code": 4 if err else 0}


_FAIL_MSG = {"timeouts": "DEADLINE_EXCEEDED: deadline exceeded after 0.500s",
             "pool_exhaustion": "RESOURCE_EXHAUSTED: connection pool exhausted",
             "partial_response": "partial response: field errors",
             "latency": "UNAVAILABLE: upstream overloaded"}


def _fail_msg(w: World, app: str) -> str:
    kinds = [d.kind for d in w.deg.get(app, ()) if d.kind in _FAIL_MSG]
    if app == w.scenario.fault.target and w.scenario.fault.kind == "cert_expiry":
        return "UNAVAILABLE: TLS handshake failed: certificate expired"
    return _FAIL_MSG[kinds[0]] if kinds else "INTERNAL: unexpected error"


def _outcomes(w: World, apps: list[str], t: np.ndarray, u: np.ndarray):
    """Per request: shed?, failed?, and the per-app failure weights."""
    n = len(t)
    perr = np.stack([w.error_p(a, t) for a in apps] + [np.full(n, BASE_ERROR)])
    shed = w.shed_p(EDGE, t)
    is_shed = u < shed
    return is_shed, (~is_shed) & (u < shed + perr.sum(axis=0)), perr


def _spans(w: World, b: _Builder, root: Node, apps: list[str], tid: str, ti: float,
           shed: bool, err: bool, col: np.ndarray, miss_target: bool):
    rng = b.rng
    if shed:
        return [(EDGE, b.host(EDGE), {
            "traceId": tid, "spanId": b.span_id(), "parentSpanId": "",
            "name": "POST /graphql", "kind": 2, "start": utc(ti), "end": utc(ti + 0.002),
            "attributes": {"http.method": "POST", "http.route": "/graphql",
                           "http.status_code": 503},
            "status": {"code": 2, "message": "load shed (503)"}})]
    fail = None
    if err:
        k = min(int(np.searchsorted(np.cumsum(col), rng.random() * col.sum())), len(apps))
        fail = apps[k] if k < len(apps) else apps[int(rng.integers(len(apps)))]
    miss = CACHE_MISS + (0.65 * float(w.fault_ramp(ti, epoch(w.scenario.fault.start_utc)))
                         if miss_target else 0.0)
    return b.build(tid, root, ti, fail, _fail_msg(w, fail) if fail else "", miss)


def _route_traces(w: World, b: _Builder, rng, route: str, root: Node, pop: Population,
                  ev_apps: set[str], ev_since: float | None,
                  fixed: tuple[World, _Builder, float] | None = None):
    """`w`/`b` are the world without the fix and the renderer's own stream, which
    every request consumes exactly as with no fix. With `fixed` = (world with the
    fix, builder on the recovery rng, time the worlds part), requests from that
    time on are also drawn there (outcomes from the same uniform), and that
    rendering is the one kept."""
    apps = sorted(_apps(root))
    target = w.scenario.fault.target
    sc = 1.0 if target is not None and target in _apps(root) else w.scale
    m = w.minutes()
    rate = ROUTE_TRACES[route] / (w.t1 - w.t0) * sc * w.diurnal(m + w.step / 2, DIURNAL_AMP)
    counts = rng.poisson(rate * w.step)
    t = np.sort(np.repeat(m, counts) + rng.random(int(counts.sum())) * w.step)
    n = len(t)
    u = rng.random(n)
    is_shed, is_err, perr = _outcomes(w, apps, t, u)
    if fixed is not None:
        wf, bf, split = fixed
        f_shed, f_err, f_perr = _outcomes(wf, apps, t, u)
    raw = rng.bytes(16 * n).hex()
    miss_target = (target in _apps(root) and w.scenario.fault.kind == "cache_eviction_storm")
    stats = {"considered": n, "kept_head": 0, "kept_error": 0}
    pop.traces_considered += n
    taken_bucket: set[int] = set()
    for i in range(n):
        tid = raw[32 * i:32 * i + 32]
        ti = float(t[i])
        shed, err = bool(is_shed[i]), bool(is_err[i])
        if head_sampled(tid, shed or err):
            spans = _spans(w, b, root, apps, tid, ti, shed, err, perr[:, i], miss_target)
        if fixed is not None and ti >= split:
            shed, err = bool(f_shed[i]), bool(f_err[i])
            if not head_sampled(tid, shed or err):
                continue
            spans = _spans(wf, bf, root, apps, tid, ti, shed, err, f_perr[:, i], miss_target)
        elif not head_sampled(tid, shed or err):
            continue
        has_error = shed or err
        if shed:
            pop.traces_shed += 1
        evidence = False
        if (not has_error and ev_since is not None and ti >= ev_since and ev_apps <= _apps(root)):
            bucket = int((ti - ev_since) // EVIDENCE_BUCKET_S)
            if bucket not in taken_bucket:
                taken_bucket.add(bucket)
                evidence = True
        if has_error:
            stats["kept_error"] += 1
            pop.traces_kept_error += 1
        else:
            stats["kept_head"] += 1
            pop.traces_kept_head += 1
        pop.evidence_traces += evidence
        pop.spans_kept += len(spans)
        yield ti, spans, evidence
    pop.by_route[route] = stats


def _resource(w: World, app: str, host: str) -> dict:
    return {"service.name": app, "host.name": host, "cloud.region": w.region(app),
            "deployment.environment": "prod", "telemetry.sdk.language": "java"}


def render_traces(scenario: Scenario, degradations: tuple[Degradation, ...],
                  rng: np.random.Generator, scale: float = 1.0,
                  population: Population | None = None) -> list[RawRecord]:
    w = World(scenario, degradations, scale)
    pop = population if population is not None else Population()
    fixed = None
    ends = [epoch(d.end_utc) for d in degradations if d.end_utc is not None]
    if ends:  # recovery: the main stream runs as without the fix (see _route_traces)
        wf = w
        w = World(scenario, tuple(d.model_copy(update={"end_utc": None})
                                  for d in degradations), scale)
        fixed = (wf, _Builder(wf, recovery_rng(scenario, "traces")), min(ends))
    b = _Builder(w, rng)
    slow = {app: epoch(at) for app, at in w.links("slow_trace").items()}
    ev_since = min(slow.values()) if slow else None
    batches: dict[tuple[str, str, str], list[dict]] = {}
    out: list[RawRecord] = []
    for route, entry in ROUTE_ENTRY.items():
        if entry not in w.apps or EDGE not in w.apps:
            continue
        root = Node(EDGE, [_tree(w, entry, route)])
        for ti, spans, evidence in _route_traces(w, b, rng, route, root, pop, set(slow),
                                                 ev_since, fixed):
            rel = f"traces/otlp/{w.hhmm(ti)}.jsonl"
            if evidence:
                per_app: dict[tuple[str, str], list[dict]] = {}
                for app, host, sp in spans:
                    per_app.setdefault((app, host), []).append(sp)
                for (app, host), sps in per_app.items():
                    out.append(_record(w, rel, app, host, sps, tags("slow_trace")))
                continue
            for app, host, sp in spans:
                key = (rel, app, host)
                batch = batches.setdefault(key, [])
                batch.append(sp)
                if len(batch) >= BATCH_SPANS:
                    out.append(_record(w, rel, app, host, batch, frozenset()))
                    batches[key] = []
    for (rel, app, host), sps in batches.items():
        if sps:
            out.append(_record(w, rel, app, host, sps, frozenset()))
    out.sort(key=lambda r: (r.rel_path, r.at_utc))
    return out


def _record(w: World, rel: str, app: str, host: str, spans: list[dict], tg) -> RawRecord:
    return RawRecord(source="traces", rel_path=rel, fmt="otlp_jsonl", host=host, tags=tg,
                     at_utc=min(s["start"] for s in spans),
                     fields={"resource": _resource(w, app, host), "scope": SCOPE,
                             "spans": list(spans)})
