"""metrics/atlas.jsonl (60 s Atlas points) and metrics/prom/<app>-<HHMM>.prom
(Prometheus exposition text scraped every 5 min from 20 apps).

Every series is a vectorized diurnal base `base*(1+DIURNAL_AMP*sin(...))`
plus Gaussian noise, bent by World's degradation effects. Evidence tags:
`metric_anomaly` latency points of each non-client chain app from its link
time, and every client.ttr.p90 point from the client link time."""
import numpy as np

from e2ekg.generate.records import RawRecord
from e2ekg.generate.world import (
    BASE_ERROR,
    CLIENT_PLATFORM,
    World,
    active,
    epoch,
    stable_unit,
    tags,
    utc,
)
from e2ekg.scenario.model import Scenario
from e2ekg.scenario.propagation import Degradation

ZIPF_S = 1.3            # (shared realism constant; unused by metrics)
DIURNAL_AMP = 0.35
TEMPLATES_PER_APP = (20, 40)  # (shared realism constant; unused by metrics)

ATLAS = "metrics/atlas.jsonl"
PROM_EVERY_S = 300
PROM_APPS = 20
NOISE = 0.03                       # relative Gaussian noise on server series
TTR_BASE_S = {"tv": 2.4, "android": 1.9, "ios": 1.7, "web": 2.1}
TTR_NOISE = {"A": 0.012, "B": 0.03}  # cell B is a 5% population: noisier p90
CELLS = ("A", "B")


def _noise(rng, n, rel):
    return 1.0 + rel * rng.standard_normal(n)


def _scaling_steps(w: World, app: str):
    cs = sorted((c for c in w.scenario.changes if c.kind == "scaling" and c.app == app),
                key=lambda c: c.at_utc)
    return [(epoch(c.at_utc), int(c.attrs["previous_capacity"]), int(c.attrs["desired_capacity"]))
            for c in cs]


def _capacity(steps, t):
    cap = np.full(len(t), float(steps[0][1]))
    for at, _, des in steps:
        cap[t >= at] = des
    return cap


def _cell_b_start(w: World) -> float:
    starts = [epoch(c.at_utc) for c in w.scenario.changes
              if c.kind == "experiment_allocation" and c.attrs.get("old_allocation_pct") == "0"]
    return min(starts) if starts else w.t0


def _retry_boost(w: World, app: str, t):
    f = w.scenario.fault
    if f.kind == "retry_storm" and any(e.callee == app for e in w.callees.get(f.target, ())):
        return 1.0 + 2.0 * w.fault_ramp(t, epoch(f.start_utc))
    return 1.0


def _server_series(w: World, rng, app: str, t):
    p50, p99, rps = w.base(app)
    d = w.diurnal(t, DIURNAL_AMP)
    n = len(t)
    extra = w.extra_ms(app, t)
    req = rps * w.n_hosts(app) * d * _retry_boost(w, app, t) * _noise(rng, n, NOISE)
    lat99 = (p99 * (1 + 0.1 * (d - 1)) + extra) * _noise(rng, n, NOISE)
    lat50 = (p50 * (1 + 0.1 * (d - 1)) + 0.5 * extra) * _noise(rng, n, NOISE)
    err = req * (BASE_ERROR + w.error_p(app, t) + w.shed_p(app, t)) * _noise(rng, n, 0.1)
    return {"server.requests.rps": req, "server.latency.p50": lat50,
            "server.latency.p99": lat99, "server.errors.rps": np.maximum(err, 0.0)}


def _atlas(w: World, rng) -> list[RawRecord]:
    t = w.minutes()
    series: list[tuple[str, dict, np.ndarray, float | None]] = []  # name, tags, values, ev_since
    anomaly = {app: epoch(at) for app, at in w.links("metric_anomaly").items()
               if not w.is_client(app)}
    for a in w.scenario.catalog:
        if w.is_client(a.id):
            continue
        base_tags = {"nf.app": a.id, "nf.region": w.region(a.id)}
        vals = _server_series(w, rng, a.id, t)
        if not a.aliases:  # fillers: rps and p99 only
            vals = {k: vals[k] for k in ("server.requests.rps", "server.latency.p99")}
        for name, v in vals.items():
            ev = anomaly.get(a.id) if name == "server.latency.p99" else None
            series.append((name, base_tags, np.round(v, 3), ev))
        steps = _scaling_steps(w, a.id)
        if steps:
            series.append(("asg.instances.inservice", base_tags, _capacity(steps, t), None))
        for e in w.callees.get(a.id, ()):
            if e.pool_size is None:
                continue
            r = sum((active(t, dg) for dg in w.degs_of(a.id, "pool_exhaustion")),
                    np.zeros(len(t)))
            r = np.minimum(r, 1.0)
            busy = 0.35 * w.diurnal(t, DIURNAL_AMP) * _noise(rng, len(t), 0.05)
            in_use = np.minimum(e.pool_size, np.round(e.pool_size * (busy + (1 - busy) * r)))
            waiting = np.round(r * (150 + 60 * rng.random(len(t))))
            ptags = {**base_tags, "target": e.callee}
            series.append(("grpc.client.pool.active", ptags, in_use, None))
            series.append(("grpc.client.pool.waiting", ptags, waiting, None))
    client_at = w.client_anomaly_at()
    client_since = epoch(client_at) if client_at else None
    b_start = _cell_b_start(w)
    for app, platform in CLIENT_PLATFORM.items():
        if app not in w.apps:
            continue
        base = TTR_BASE_S[platform] * w.diurnal(t, DIURNAL_AMP) + w.client_extra_s(app, t)
        for cell in CELLS:
            v = base * _noise(rng, len(t), TTR_NOISE[cell])
            if cell == "B":
                v = np.where(t >= b_start, v, np.nan)
            series.append(("client.ttr.p90", {"nf.app": app, "platform": platform,
                                              "ab.cell": cell}, np.round(v, 4), client_since))
    out = []
    for i, ti in enumerate(t):
        at = utc(ti)
        for name, stags, v, ev in series:
            if np.isnan(v[i]):
                continue
            out.append(RawRecord(
                source="atlas", rel_path=ATLAS, fmt="atlas_jsonl",
                fields={"name": name, "tags": stags, "t": at, "v": float(v[i])}, at_utc=at,
                tags=tags("metric_anomaly") if ev is not None and ti >= ev else frozenset()))
    return out


def prom_apps(w: World) -> list[str]:
    named = [a.id for a in w.scenario.catalog if a.aliases and not w.is_client(a.id)]
    fillers = sorted(a.id for a in w.scenario.catalog if not a.aliases)
    return (named + fillers)[:PROM_APPS]


def _prom_app(w: World, rng, app: str) -> list[RawRecord]:
    t = np.arange(w.t0, w.t1, PROM_EVERY_S)
    n = len(t)
    vals = _server_series(w, rng, app, t)
    hosts = w.hosts(app)
    ok_rate = (vals["server.requests.rps"] - vals["server.errors.rps"]) / len(hosts)
    err_rate = vals["server.errors.rps"] / len(hosts)
    per_host = []
    for host in hosts:
        start = 1e6 * (1 + 9 * stable_unit("ctr", app, host))
        per_host.append({
            "ok": np.floor(start + np.cumsum(ok_rate * PROM_EVERY_S)).astype(np.int64),
            "err": np.floor(start / 1000 + np.cumsum(err_rate * PROM_EVERY_S)).astype(np.int64),
            "cpu": np.round(start / 1e4 + np.cumsum(PROM_EVERY_S * (0.4 + 0.3 * rng.random(n))), 2),
            "heap": np.round(2.1e9 + 6e8 * rng.random(n)).astype(np.int64),
            "busy": 0.3 + 0.2 * rng.random(n), "wait": 150 + 60 * rng.random(n)})
    pools = [e for e in w.callees.get(app, ()) if e.pool_size is not None]
    exhausted = np.minimum(sum((active(t, d)
                                for d in w.degs_of(app, "pool_exhaustion")), np.zeros(n)), 1.0)
    steps = _scaling_steps(w, app)
    cap = _capacity(steps, t) if steps else None
    out: list[RawRecord] = []
    for k, ti in enumerate(t):
        at = utc(ti)
        rel = f"metrics/prom/{app}-{w.hhmm(ti)}.prom"

        def fam(name, typ, text, samples):
            out.append(_prom(rel, at, None, {"kind": "help", "name": name, "text": text}))
            out.append(_prom(rel, at, None, {"kind": "type", "name": name, "type": typ}))
            for host, labels, value in samples:
                out.append(_prom(rel, at, host, {"kind": "sample", "name": name,
                                                 "labels": labels, "value": value, "t": at}))
        lab = [(h, {"app": app, "instance": h}, ph) for h, ph in zip(hosts, per_host)]
        fam("requests_total", "counter", "Total requests served.",
            [(h, {**lb, "status": s}, int(ph[key][k]))
             for h, lb, ph in lab for s, key in (("2xx", "ok"), ("5xx", "err"))])
        fam("request_latency_ms", "gauge", "Request latency quantiles in milliseconds.",
            [(h, {**lb, "quantile": q}, round(float(vals[name][k]), 2)) for h, lb, _ in lab
             for q, name in (("0.5", "server.latency.p50"), ("0.99", "server.latency.p99"))])
        fam("process_cpu_seconds_total", "counter", "Total user and system CPU time spent.",
            [(h, lb, float(ph["cpu"][k])) for h, lb, ph in lab])
        fam("jvm_memory_used_bytes", "gauge", "Used bytes of a given JVM memory area.",
            [(h, {**lb, "area": "heap"}, int(ph["heap"][k])) for h, lb, ph in lab])
        for e in pools:
            samples = []
            for h, lb, ph in lab:
                x = float(exhausted[k])
                in_use = e.pool_size if x > 0.5 else int(e.pool_size * ph["busy"][k])
                waiting = round(x * float(ph["wait"][k]))
                samples += [(h, {**lb, "target": e.callee, "state": "active"}, in_use),
                            (h, {**lb, "target": e.callee, "state": "idle"}, e.pool_size - in_use),
                            (h, {**lb, "target": e.callee, "state": "waiting"}, waiting)]
            fam("grpc_client_pool_connections", "gauge", "Client connection pool by state.",
                samples)
        if cap is not None:
            asg = next(c.attrs.get("asg", f"{app}-v001") for c in w.scenario.changes
                       if c.kind == "scaling" and c.app == app)
            fam("asg_desired_capacity", "gauge", "Desired capacity of the auto scaling group.",
                [(None, {"app": app, "asg": asg}, int(cap[k]))])
        fam("up", "gauge", "1 if the scrape target is up.", [(h, lb, 1) for h, lb, _ in lab])
    return out


def _prom(rel, at, host, fields) -> RawRecord:
    return RawRecord(source="prom", rel_path=rel, fmt="prom", fields=fields, at_utc=at, host=host)


def render_metrics(scenario: Scenario, degradations: tuple[Degradation, ...],
                   rng: np.random.Generator, scale: float = 1.0) -> list[RawRecord]:
    w = World(scenario, degradations, scale)
    out = _atlas(w, rng)
    for app in prom_apps(w):
        out += _prom_app(w, rng, app)
    return out
