"""The shared world model behind every renderer.

One place decides time, hosts, base load, how each Degradation from
propagate() bends latency and error rates, and which records are answer-key
evidence, so metrics, logs, traces, events and chat agree with each other.
Nothing here draws from an rng: app-specific constants come from a stable
hash, and every renderer brings its own seeded rng for the noise."""
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np

from e2ekg.scenario.model import CHANGE_SIGNAL, Change, DependencyEdge, Scenario
from e2ekg.scenario.propagation import Degradation
from e2ekg.timeutil import PT

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
RAMP_S = 120                # a degradation reaches full strength 2 min after its start
POOL_WAIT_MS = 900          # connection-pool queueing once a pool is exhausted
TIMEOUT_WAIT_MS = 150       # retries/queueing added per timing-out hop
TARGET_FLOOR_MS = 40        # minimum extra latency on a fault target
SHED_PER_MAGNITUDE = 0.05   # load_shed magnitude 0.3 -> 1.5% of requests shed
CLIENT_PLATFORM = {"client-tv": "tv", "client-android": "android", "client-ios": "ios",
                   "client-web": "web"}
# Extra per-request error probability at full ramp, by degradation kind.
DEG_ERROR = {"timeouts": 0.004, "pool_exhaustion": 0.005, "partial_response": 0.003,
             "latency": 0.0, "errors": 0.02, "load_shed": 0.0}
# Extra error probability on the fault target itself, by fault kind.
FAULT_ERROR = {"capacity_reduction": 0.0, "bad_deploy": 0.04, "cache_eviction_storm": 0.0,
               "db_hot_partition": 0.01, "cert_expiry": 0.15, "retry_storm": 0.01, "none": 0.0}
BASE_ERROR = 0.001

# app -> (p50 ms, p99 ms, requests/s per host)
BASE: dict[str, tuple[float, float, float]] = {
    "edge-gateway": (8, 140, 4200), "homepage-graphql": (35, 110, 1900),
    "lolomo-svc": (25, 80, 2100), "row-ranker": (15, 45, 2300),
    "title-batch-loader": (9, 25, 2400), "title-metadata-svc": (6, 40, 9000),
    "artwork-svc": (6, 30, 5000), "artwork-metadata-svc": (4, 20, 3000),
    "entitlement-svc": (5, 25, 3000), "api-gateway": (12, 90, 2500),
    "playback-api": (20, 120, 1200), "ab-allocator": (2, 9, 6000),
    "evcache-titles": (0.6, 2, 30000), "cassandra-titles": (2, 12, 4000),
    "ratings-svc": (4, 18, 2000), "profile-svc": (4, 22, 4500), "search-svc": (25, 160, 800),
}


def stable_unit(*parts: object) -> float:
    """A deterministic number in [0, 1) for an app-specific constant."""
    h = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return int(h[:12], 16) / float(1 << 48)


def epoch(dt: datetime) -> float:
    return (dt - EPOCH).total_seconds()


def utc(sec: float) -> datetime:
    """Epoch seconds -> aware UTC datetime, rounded to the millisecond."""
    return EPOCH + timedelta(milliseconds=round(sec * 1000))


def ramp(t: np.ndarray, start: float) -> np.ndarray:
    return np.clip((np.asarray(t, dtype=float) - start) / RAMP_S, 0.0, 1.0)


@dataclass
class World:
    scenario: Scenario
    degs: tuple[Degradation, ...]
    scale: float = 1.0
    apps: dict = field(init=False)
    callees: dict[str, list[DependencyEdge]] = field(init=False)
    callers: dict[str, list[str]] = field(init=False)
    deg: dict[str, list[Degradation]] = field(init=False)
    path: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        s = self.scenario
        self.t0, self.t1 = epoch(s.window.start_utc), epoch(s.window.end_utc)
        self.step = s.window.metric_step_s
        self.apps = {a.id: a for a in s.catalog}
        self.callees, self.callers, self.deg = {}, {}, {}
        for e in s.edges:
            self.callees.setdefault(e.caller, []).append(e)
            self.callers.setdefault(e.callee, []).append(e.caller)
        for d in self.degs:
            self.deg.setdefault(d.app, []).append(d)
        self.path = frozenset(self.deg) | (
            {s.fault.target} if s.fault.target is not None else frozenset())
        self.local_offset_s = s.window.start_utc.astimezone(PT).utcoffset().total_seconds()
        self._grid = np.arange(self.t0, self.t1, 1.0)
        self._extra: dict[str, np.ndarray] = {}

    # ---- time ----------------------------------------------------------
    def minutes(self) -> np.ndarray:
        return np.arange(self.t0, self.t1, self.step)

    def hhmm(self, sec: float) -> str:
        return utc(sec).astimezone(PT).strftime("%H%M")

    def diurnal(self, t: np.ndarray, amp: float) -> np.ndarray:
        """Evening-peaking day curve (max at 20:00 local)."""
        hours = (np.asarray(t, dtype=float) + self.local_offset_s) / 3600.0
        return 1.0 + amp * np.sin(2 * np.pi * (hours - 14.0) / 24.0)

    # ---- apps ----------------------------------------------------------
    def scale_for(self, app: str) -> float:
        """Background traffic is scaled only off the propagation path (R4)."""
        return 1.0 if app in self.path else self.scale

    def base(self, app: str) -> tuple[float, float, float]:
        if app in BASE:
            return BASE[app]
        p50 = 3 + 27 * stable_unit("p50", app)
        return p50, p50 * (3 + 2 * stable_unit("tail", app)), 50 + 1450 * stable_unit("rps", app)

    def n_hosts(self, app: str) -> int:
        a = self.apps[app]
        if a.aliases:  # named apps
            return 3 if a.tier == 0 else 2
        return 1 + (stable_unit("hosts", app) < 0.5)

    def hosts(self, app: str) -> list[str]:
        return [f"i-0{hashlib.sha256(f'{app}#{k}'.encode()).hexdigest()[:16]}"
                for k in range(self.n_hosts(app))]

    def region(self, app: str) -> str:
        return self.apps[app].regions[0]

    def is_client(self, app: str) -> bool:
        return self.apps[app].kind == "client"

    # ---- degradation effects ------------------------------------------
    def degs_of(self, app: str, kind: str | None = None) -> list[Degradation]:
        return [d for d in self.deg.get(app, ()) if kind is None or d.kind == kind]

    def own_extra_ms(self, app: str, t: np.ndarray) -> np.ndarray:
        out = np.zeros(np.shape(t))
        if self.is_client(app):
            return out
        for d in self.deg.get(app, ()):
            r = ramp(t, epoch(d.start_utc))
            if d.kind == "latency":
                out += max(TARGET_FLOOR_MS, self.base(app)[1] * d.magnitude) * r
            elif d.kind == "pool_exhaustion":
                out += POOL_WAIT_MS * r
            elif d.kind == "timeouts":
                out += TIMEOUT_WAIT_MS * r
        return out

    def extra_ms(self, app: str, t: np.ndarray) -> np.ndarray:
        """Latency added to a request served by `app`: its own extra plus the
        worst degraded callee's (calls fan out in parallel)."""
        if app not in self._extra:
            own = self.own_extra_ms(app, self._grid)
            down = [self.extra_ms(e.callee, self._grid) for e in self.callees.get(app, ())
                    if e.callee in self.path and not self.is_client(e.callee)]
            self._extra[app] = own + (np.max(down, axis=0) if down else 0.0)
        return self._extra[app][self.grid_index(t)]

    def grid_index(self, t) -> np.ndarray:
        return np.clip((np.asarray(t, dtype=float) - self.t0).astype(int), 0, len(self._grid) - 1)

    def own_extra_grid(self, app: str) -> np.ndarray:
        return self.own_extra_ms(app, self._grid)

    def error_p(self, app: str, t: np.ndarray) -> np.ndarray:
        """Extra probability that a request fails *at* `app`."""
        out = np.zeros(np.shape(t))
        for d in self.deg.get(app, ()):
            out += DEG_ERROR.get(d.kind, 0.0) * ramp(t, epoch(d.start_utc))
        f = self.scenario.fault
        if app == f.target:
            onset = min(epoch(d.start_utc) for d in self.deg[app]) if app in self.deg else None
            if onset is not None:
                out += FAULT_ERROR[f.kind] * ramp(t, onset)
        return out

    def route_error_p(self, app: str, t: np.ndarray) -> np.ndarray:
        """Probability a request entering at `app` fails anywhere downstream."""
        seen, todo, out = set(), [app], np.zeros(np.shape(t))
        while todo:
            a = todo.pop()
            if a in seen:
                continue
            seen.add(a)
            out += self.error_p(a, t)
            todo += [e.callee for e in self.callees.get(a, ()) if e.callee in self.path]
        return out

    def degraded_callee(self, app: str) -> DependencyEdge | None:
        """The callee edge through which `app` feels the fault, if any."""
        cands = [e for e in self.callees.get(app, ()) if e.callee in self.path]
        return min(cands, key=lambda e: (e.pool_size is None, e.callee)) if cands else None

    def shed_p(self, app: str, t: np.ndarray) -> np.ndarray:
        out = np.zeros(np.shape(t))
        for d in self.degs_of(app, "load_shed"):
            out += SHED_PER_MAGNITUDE * d.magnitude * ramp(t, epoch(d.start_utc))
        return out

    def client_extra_s(self, app: str, t: np.ndarray) -> np.ndarray:
        out = np.zeros(np.shape(t))
        for d in self.degs_of(app, "latency"):
            out += d.magnitude * ramp(t, epoch(d.start_utc))
        return out

    # ---- evidence (answer_key.causal_chain) ---------------------------
    def link_at(self, signal: str, app: str) -> datetime | None:
        ats = [lk.at_utc for lk in self.scenario.answer_key.causal_chain
               if lk.signal == signal and lk.app == app]
        return min(ats) if ats else None

    def links(self, signal: str) -> dict[str, datetime]:
        out: dict[str, datetime] = {}
        for lk in self.scenario.answer_key.causal_chain:
            if lk.signal == signal and (lk.app not in out or lk.at_utc < out[lk.app]):
                out[lk.app] = lk.at_utc
        return out

    def change_tags(self, c: Change) -> frozenset[str]:
        sig = CHANGE_SIGNAL[c.kind]
        at = self.link_at(sig, c.app)
        if at is None:
            return frozenset()
        hit = c.at_utc == at or (sig == "autoscaling" and c.at_utc >= at)
        return frozenset({f"evidence:{sig}"}) if hit else frozenset()

    def client_anomaly_at(self) -> datetime | None:
        ats = [at for app, at in self.links("metric_anomaly").items() if self.is_client(app)]
        return min(ats) if ats else None


def tags(*names: str) -> frozenset[str]:
    return frozenset(f"evidence:{n}" for n in names)


AS_OF_AFTER_ALERT = timedelta(minutes=30)   # R21: the RCA runs as of alert + 30 min
MITIGATION_MIN = 40                         # "bump min to 40"
MITIGATION_OFFSETS = (timedelta(minutes=2), timedelta(minutes=31))  # after as_of
MITIGATION_RESET_AFTER = timedelta(seconds=50)


def as_of_utc(s: Scenario) -> datetime | None:
    alerts = [lk.at_utc for lk in s.answer_key.causal_chain if lk.signal == "alert"]
    return min(alerts) + AS_OF_AFTER_ALERT if alerts else None


@dataclass(frozen=True)
class Mitigation:
    """A failed manual capacity bump that the fast-property poller undoes (R23)."""
    app: str
    asg: str
    prop: str
    at_utc: datetime
    reset_utc: datetime
    floor: int
    bumped_to: int


def mitigation_attempts(s: Scenario) -> tuple[Mitigation, ...]:
    """Renderer-side mitigation events, not Scenario changes: on a capacity
    reduction driven by an `*.asg.minInstances` fast property, responders bump
    the ASG to MITIGATION_MIN twice after the cutoff and the poller resets it."""
    f, cut = s.fault, as_of_utc(s)
    if f.kind != "capacity_reduction" or cut is None:
        return ()
    prop = next((c for c in s.changes if c.kind == "config_change" and c.app == f.target
                 and c.attrs.get("property", "").endswith(".asg.minInstances")), None)
    scales = sorted((c for c in s.changes if c.kind == "scaling" and c.app == f.target),
                    key=lambda c: c.at_utc)
    if prop is None or not scales:
        return ()
    floor = int(scales[-1].attrs["desired_capacity"])
    return tuple(Mitigation(app=f.target, asg=scales[-1].attrs.get("asg", f"{f.target}-v001"),
                            prop=prop.attrs["property"], at_utc=cut + off,
                            reset_utc=cut + off + MITIGATION_RESET_AFTER, floor=floor,
                            bumped_to=MITIGATION_MIN)
                 for off in MITIGATION_OFFSETS if cut + off < s.window.end_utc)
