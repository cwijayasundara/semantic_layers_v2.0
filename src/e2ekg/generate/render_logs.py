"""logs/<app>/<host>.log in the app's own format (LOG_FORMAT, default json).

Background lines are a diurnal Poisson stream per host whose templates are
Zipf(ZIPF_S) over 20-40 templates picked per app. On top of that, planted
lines that the scenario explains:
  * degradation lines from propagate() (pool exhaustion, deadline exceeded,
    partial responses, the fault target's own symptom), tagged
    `evidence:log_error` where the causal chain has a log_error link;
  * the chronic artwork-svc NullPointerException at a constant cadence;
  * a short error blip on every deployed app right after its deployment;
  * caller/callee request-id pairs (what Task 6's skew estimator matches).
Background volume of apps off the propagation path is multiplied by `scale`;
planted lines never are."""
import hashlib
import string

import numpy as np

from e2ekg.generate.log_templates import (
    ANDROID_PREVIOUS,
    DEPLOY_BLIP,
    EXTRA,
    FAULT_LINES,
    GENERIC,
    NPE_MSG,
    NPE_STACK,
    PLACEHOLDERS,
    ROUTES,
    THREADS,
    USER_AGENTS,
    Template,
)
from e2ekg.generate.records import RawRecord
from e2ekg.generate.world import World, epoch, ramp, tags, utc
from e2ekg.scenario.model import Scenario
from e2ekg.scenario.propagation import Degradation

ZIPF_S = 1.3
DIURNAL_AMP = 0.35
TEMPLATES_PER_APP = (20, 40)

LOG_FORMAT = {"title-batch-loader": "log4j", "row-ranker": "logfmt", "edge-gateway": "nginx",
              "homepage-graphql": "json", "cassandra-titles": "log4j", "playback-api": "log4j",
              "entitlement-svc": "log4j", "ab-allocator": "logfmt", "search-svc": "logfmt"}
FILLER_FORMATS = ("json", "json", "logfmt", "log4j")
# Background lines per second per host.
LINES_PER_HOST_S = {"edge-gateway": 1.6, "homepage-graphql": 1.0, "lolomo-svc": 1.0,
                    "row-ranker": 1.2, "title-batch-loader": 1.0, "title-metadata-svc": 0.8}
NAMED_RATE, FILLER_RATE = 1.6, 0.3
# Planted degradation lines per second per host at full ramp.
DEG_LINE_RATE = {"pool_exhaustion": 0.25, "timeouts": 0.3, "partial_response": 0.3,
                 "latency": 0.2}
NPE_APP, NPE_PERIOD_S, NPE_JITTER_S = "artwork-svc", 15.0, 3.0
BLIP_S, BLIP_RATE = 180, 1.5
PAIR_RATE = 0.2
PLATFORM_MIX = (("tv", 0.35), ("android", 0.25), ("ios", 0.2), ("web", 0.2))
ENTRY = (("/graphql", "homepage-graphql"), ("/api/playback", "playback-api"),
         ("/api/search", "search-svc"))
EDGE_TIMEOUT_S = 2.0


def zipf_probs(n: int, s: float = ZIPF_S) -> np.ndarray:
    w = 1.0 / np.arange(1, n + 1) ** s
    return w / w.sum()


def log_format(w: World, app: str) -> str:
    if app in LOG_FORMAT:
        return LOG_FORMAT[app]
    if w.apps[app].aliases:
        return "json"
    h = int(hashlib.sha256(app.encode()).hexdigest()[:4], 16)
    return FILLER_FORMATS[h % len(FILLER_FORMATS)]


def _app_type(w: World, app: str) -> str | None:
    kind = w.apps[app].kind
    if app == "homepage-graphql":
        return "graphql"
    if kind in ("datastore", "cache"):
        return kind
    return "gateway" if kind == "edge" else None


def _ctx(w: World, app: str) -> dict:
    base = "".join(p.capitalize() for p in app.split("-") if p not in ("svc", "api"))
    return {"deps": [e.callee for e in w.callees.get(app, ())] or ["localhost"],
            "callers": w.callers.get(app) or ["edge-gateway"],
            "rpcs": [f"Get{base}", f"BatchGet{base}", f"List{base}", f"Update{base}"],
            "region": w.region(app), "pkg": app.replace("-", "")}


_FMT = string.Formatter()


def _render(text: str, a: int, b: int, c: int, ctx: dict) -> str:
    keys = [f for _, f, _, _ in _FMT.parse(text) if f]
    return text.format(**{k: PLACEHOLDERS[k](a, b, c, ctx) for k in keys}) if keys else text


class _Lines:
    """Planted lines for one host: (t, level, component, msg, extra, stack, tags)."""

    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def add(self, t, level, comp, msg, extra=None, stack=(), tg=frozenset()):
        self.rows.append((float(t), level, comp, msg, extra or {}, tuple(stack), tg))


def _poisson_times(rng, w: World, rate: np.ndarray | float) -> np.ndarray:
    m = w.minutes()
    counts = rng.poisson(np.broadcast_to(rate, m.shape) * w.step)
    t = np.repeat(m, counts) + rng.random(int(counts.sum())) * w.step
    return np.sort(t)


def _fields(fmt: str, at, level, logger, thread, msg, app, host, extra, stack) -> dict:
    if fmt == "json":
        f = {"ts": at, "level": level, "app": app, "host": host, "logger": logger,
             "thread": thread, "msg": msg, **extra}
        if stack:
            f["stack_trace"] = "\n".join(stack)
        return f
    if fmt == "logfmt":
        f = {"ts": at, "level": level.lower(), "app": app, "caller": logger.rsplit(".", 1)[-1],
             "msg": msg, **extra}
        if stack:
            f["error"] = stack[0]
        return f
    if extra:
        msg = msg + " " + " ".join(f"{k}={v}" for k, v in extra.items())
    f = {"ts": at, "level": level, "thread": thread, "logger": logger, "msg": msg}
    if stack:
        f["stack"] = list(stack)
    return f


def _app_templates(rng, app_type: str | None) -> list[Template]:
    bank = list(GENERIC) + list(EXTRA.get(app_type, ()))
    n = min(int(rng.integers(TEMPLATES_PER_APP[0], TEMPLATES_PER_APP[1] + 1)), len(bank))
    chosen = [bank[i] for i in rng.permutation(len(bank))[:n]]
    rank = {"INFO": 0, "DEBUG": 1, "WARN": 2, "ERROR": 3}
    return sorted(chosen, key=lambda t: rank[t[0]])  # stable: shuffled within a level


def _host_records(w: World, rng, app: str, host: str, fmt: str, templates, ctx,
                  bg_t: np.ndarray, planted: _Lines) -> list[RawRecord]:
    rel = f"logs/{app}/{host}.log"
    n = len(bg_t)
    tmpl = rng.choice(len(templates), size=n, p=zipf_probs(len(templates)))
    abc = rng.integers(0, 2 ** 31, size=(n, 3))
    rows = sorted(planted.rows, key=lambda r: r[0])
    pt = np.array([r[0] for r in rows])
    order = np.argsort(np.concatenate([bg_t, pt]), kind="stable")
    pkg = ctx["pkg"]
    out = []
    for i in order.tolist():
        if i < n:
            level, comp, text = templates[tmpl[i]]
            a, b, c = abc[i].tolist()
            msg, extra, stack, tg, t = _render(text, a, b, c, ctx), {}, (), frozenset(), bg_t[i]
            thread = THREADS[c % len(THREADS)].format(k=a % 16 + 1)
        else:
            t, level, comp, msg, extra, stack, tg = rows[i - n]
            thread = THREADS[0].format(k=int(t * 1000) % 16 + 1)
        at = utc(t)
        out.append(RawRecord(source="logs", rel_path=rel, fmt=fmt, at_utc=at, host=host, tags=tg,
                             fields=_fields(fmt, at, level, f"com.netflix.{pkg}.{comp}", thread,
                                            msg, app, host, extra, stack)))
    return out


def _edge_records(w: World, rng, app: str, host: str, bg_t: np.ndarray) -> list[RawRecord]:
    rel = f"logs/{app}/{host}.log"
    n = len(bg_t)
    k = min(int(rng.integers(TEMPLATES_PER_APP[0], TEMPLATES_PER_APP[1] + 1)), len(ROUTES))
    routes = [ROUTES[i] for i in sorted(rng.permutation(len(ROUTES))[:k])]
    ri = rng.choice(k, size=n, p=zipf_probs(k))
    abc = rng.integers(0, 2 ** 31, size=(n, 3))
    u = rng.random(n)
    jitter = np.exp(0.4 * rng.standard_normal(n))
    plat = rng.choice(len(PLATFORM_MIX), size=n, p=[p for _, p in PLATFORM_MIX])
    entry_err, entry_extra = {}, {}
    for prefix, entry in ENTRY:
        if entry in w.apps:
            entry_err[prefix] = w.route_error_p(entry, bg_t) + w.error_p(app, bg_t)
            entry_extra[prefix] = w.extra_ms(entry, bg_t) / 1000.0
    shed = w.shed_p(app, bg_t)
    rel_at = {c.app: (epoch(c.at_utc), c.attrs.get("version"))
              for c in w.scenario.changes if c.kind == "client_release"}
    android_at, android_v = rel_at.get("client-android", (float("inf"), ANDROID_PREVIOUS))
    out = []
    for i in range(n):
        method, path, status, size, rt, _ = routes[ri[i]]
        a, b, c = abc[i].tolist()
        t = bg_t[i]
        prefix = next((p for p, _ in ENTRY if path.startswith(p)), None)
        err = entry_err[prefix][i] if prefix in entry_err else 0.0
        rt_s = rt * jitter[i] + (entry_extra[prefix][i] if prefix in entry_extra else 0.0)
        if status < 400 and u[i] < err:
            status, size, rt_s = 504, 160, EDGE_TIMEOUT_S + 0.01 * jitter[i]
        elif status < 400 and u[i] < err + shed[i]:
            status, size, rt_s = 503, 0, 0.002 * jitter[i]
        platform = PLATFORM_MIX[plat[i]][0]
        ua = USER_AGENTS[platform].format(
            version=android_v if t >= android_at else ANDROID_PREVIOUS)
        at = utc(t)
        out.append(RawRecord(source="logs", rel_path=rel, fmt="nginx", at_utc=at, host=host,
                             fields={
            "remote_addr": f"100.{64 + a % 64}.{b % 256}.{c % 256}", "remote_user": "-",
            "ts": at, "method": method, "path": _render(path, a, b, c, {}),
            "protocol": "HTTP/1.1", "status": status,
            "bytes": int(size * (0.7 + 0.6 * (a % 1000) / 1000)) if size else 0,
            "referer": "-", "user_agent": ua, "request_time": round(float(rt_s), 3)}))
    return out


def _deg_line(w: World, app: str, d: Degradation, a: int, b: int, prog: float):
    """(level, component, msg, stack) for one planted degradation line."""
    e = w.degraded_callee(app)
    callee = e.callee if e else "upstream"
    cpkg = "".join(p.capitalize() for p in callee.split("-"))
    pkg = app.replace("-", "")
    if d.kind == "pool_exhaustion":
        pool = e.pool_size if e and e.pool_size else 64
        waiting = int(prog * 150) + a % 40
        return ("ERROR", f"client.{cpkg}Client",
                f"Connection pool exhausted (active={pool}, idle=0, waiting={waiting}) "
                f"calling {callee}",
                ("io.grpc.StatusRuntimeException: RESOURCE_EXHAUSTED: connection pool exhausted",
                 "\tat io.grpc.stub.ClientCalls.toStatusRuntimeException(ClientCalls.java:268)",
                 f"\tat com.netflix.{pkg}.client.{cpkg}Client.batchGet({cpkg}Client.java:88)",
                 f"\tat com.netflix.{pkg}.batch.BatchLoader.load(BatchLoader.java:41)"))
    if d.kind == "timeouts":
        deadline = 500 if w.apps[app].kind != "edge" else 2000
        return ("ERROR", f"client.{cpkg}Client",
                f"Deadline exceeded calling {callee} after {deadline}ms",
                (f"io.grpc.StatusRuntimeException: DEADLINE_EXCEEDED: deadline exceeded after "
                 f"{deadline / 1000:.3f}s",
                 "\tat io.grpc.stub.ClientCalls.toStatusRuntimeException(ClientCalls.java:268)",
                 f"\tat com.netflix.{pkg}.client.{cpkg}Client.call({cpkg}Client.java:131)"))
    if d.kind == "partial_response":
        m = 12 + b % 8
        return ("WARN", "graphql.Executor",
                f"Partial response for query HomePage: {1 + b % 4} of {m} fields resolved "
                f"with errors (path: {callee})", ())
    lines = FAULT_LINES.get(w.scenario.fault.kind, FAULT_LINES["capacity_reduction"])
    level, comp, text = lines[a % len(lines)]
    return level, comp, _render(text, a, b, a ^ b, _ctx(w, app)), ()


def _plant_degradations(w: World, rng, app: str, hosts, planted) -> None:
    ev_at = w.link_at("log_error", app)
    for d in w.deg.get(app, ()):
        if d.kind not in DEG_LINE_RATE or w.is_client(app):
            continue
        start = epoch(d.start_utc)
        for h in hosts:
            m = w.minutes()
            t = _poisson_times(rng, w, DEG_LINE_RATE[d.kind] * ramp(m + w.step / 2, start))
            t = t[t >= start]
            ab = rng.integers(0, 2 ** 31, size=(len(t), 2))
            for ti, (a, b) in zip(t.tolist(), ab.tolist()):
                level, comp, msg, stack = _deg_line(w, app, d, a, b, float(ramp(ti, start)))
                tg = tags("log_error") if ev_at is not None and ti >= epoch(ev_at) else frozenset()
                planted[h].add(ti, level, comp, msg, stack=stack, tg=tg)


def _plant_npe(w: World, rng, hosts, planted) -> None:
    for k, h in enumerate(hosts):
        t = np.arange(w.t0 + NPE_PERIOD_S * (k + 1) / (len(hosts) + 1), w.t1, NPE_PERIOD_S)
        t = np.clip(t + rng.uniform(-NPE_JITTER_S, NPE_JITTER_S, len(t)), w.t0, w.t1 - 0.001)
        for ti, b in zip(t.tolist(), rng.integers(0, 2 ** 31, len(t)).tolist()):
            planted[h].add(ti, "ERROR", "server.ArtworkHandler",
                           NPE_MSG.format(title=80_000_000 + (b * 7919) % 1_000_000),
                           stack=NPE_STACK)


def _plant_deploy_blips(w: World, rng, app: str, hosts, planted, ctx) -> None:
    for c in w.scenario.changes:
        if c.kind != "deployment" or c.app != app:
            continue
        start = epoch(c.at_utc)
        for h in hosts:
            n = rng.poisson(BLIP_RATE * BLIP_S)
            t = np.sort(start + 5 + rng.random(n) * BLIP_S)
            for ti, (a, b) in zip(t.tolist(), rng.integers(0, 2 ** 31, (n, 2)).tolist()):
                if ti >= w.t1:
                    continue
                level, comp, text = DEPLOY_BLIP[a % len(DEPLOY_BLIP)]
                planted[h].add(ti, level, comp, _render(text, a, b, a ^ b, ctx))


def _plant_pairs(w: World, rng, logging: dict[str, list[str]], planted) -> None:
    for e in w.scenario.edges:
        if e.caller not in logging or e.callee not in logging:
            continue
        if "nginx" in (log_format(w, e.caller), log_format(w, e.callee)):
            continue
        if not (w.apps[e.caller].aliases and w.apps[e.callee].aliases):
            continue
        rate = PAIR_RATE * min(w.scale_for(e.caller), w.scale_for(e.callee))
        t = _poisson_times(rng, w, rate)
        n = len(t)
        hc = rng.integers(0, len(logging[e.caller]), n)
        he = rng.integers(0, len(logging[e.callee]), n)
        lat = 0.002 + 0.006 * rng.random(n)
        rid = rng.integers(0, 2 ** 63, n)
        rpcs = _ctx(w, e.callee)["rpcs"]
        for i in range(n):
            r = {"request_id": f"{int(rid[i]):016x}"}
            rpc = rpcs[int(rid[i]) % len(rpcs)]
            planted[logging[e.caller][hc[i]]].add(t[i], "INFO", "client.UpstreamClient",
                                                  f"Calling {e.callee}.{rpc}", extra=r)
            if t[i] + lat[i] < w.t1:
                planted[logging[e.callee][he[i]]].add(t[i] + lat[i], "INFO",
                                                      "server.RequestHandler",
                                                      f"Received {rpc} from {e.caller}", extra=r)


def logging_apps(w: World) -> list[str]:
    return [a.id for a in w.scenario.catalog if not w.is_client(a.id)]


def render_logs(scenario: Scenario, degradations: tuple[Degradation, ...],
                rng: np.random.Generator, scale: float = 1.0) -> list[RawRecord]:
    w = World(scenario, degradations, scale)
    apps = logging_apps(w)
    hosts = {app: w.hosts(app) for app in apps}
    planted = {h: _Lines() for hs in hosts.values() for h in hs}
    _plant_pairs(w, rng, hosts, planted)
    out: list[RawRecord] = []
    for app in apps:
        fmt, ctx = log_format(w, app), _ctx(w, app)
        if fmt != "nginx":
            _plant_degradations(w, rng, app, hosts[app], planted)
            if app == NPE_APP:
                _plant_npe(w, rng, hosts[app], planted)
            _plant_deploy_blips(w, rng, app, hosts[app], planted, ctx)
        base = LINES_PER_HOST_S.get(app, NAMED_RATE if w.apps[app].aliases else FILLER_RATE)
        rate = base * w.scale_for(app) * w.diurnal(w.minutes() + w.step / 2, DIURNAL_AMP)
        templates = _app_templates(rng, _app_type(w, app))
        for h in hosts[app]:
            bg = _poisson_times(rng, w, rate)
            if fmt == "nginx":  # the edge's symptoms are its status codes, not extra lines
                out += _edge_records(w, rng, app, h, bg)
            else:
                out += _host_records(w, rng, app, h, fmt, templates, ctx, bg, planted[h])
    return out
