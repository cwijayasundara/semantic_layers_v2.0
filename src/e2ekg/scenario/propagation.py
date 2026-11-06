"""Deterministic fault propagation up the caller chain. No rng."""
from collections import deque
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from e2ekg.scenario.model import AppId, Fault, FaultKind, Window

DegradationKind = Literal["latency", "errors", "pool_exhaustion", "timeouts",
                          "partial_response", "load_shed"]

ONSET_DELAY_S: dict[FaultKind, int] = {
    "capacity_reduction": 780, "bad_deploy": 120, "cache_eviction_storm": 240,
    "db_hot_partition": 300, "cert_expiry": 60, "retry_storm": 180, "none": 0,
}
# Cumulative lag by caller hop depth (depth 1 uses HOP_LAG_S[0], ...).
HOP_LAG_S = (180, 150, 180, 120, 150, 120)
LOAD_SHED_AFTER_CLIENTS_S = 60
CLIENT_LATENCY_MAGNITUDE = 1.5  # seconds added to TTR p90


class Degradation(BaseModel):
    model_config = ConfigDict(frozen=True)
    app: AppId
    start_utc: datetime
    kind: DegradationKind
    magnitude: float


def _kind_at(app: str, out_edges, depth: dict[str, int], is_root: bool) -> tuple[DegradationKind, float]:
    toward = [e for e in out_edges.get(app, ()) if depth.get(e.callee, 1 << 30) < depth[app]]
    if is_root:
        return "latency", CLIENT_LATENCY_MAGNITUDE
    if any(e.pool_size is not None for e in toward):
        return "pool_exhaustion", 1.0
    if any(e.protocol == "graphql" for e in toward):  # explicit: graphql callers degrade partially
        return "partial_response", 0.5
    return "timeouts", 1.0


def propagate(fault: Fault, edges, window: Window) -> tuple[Degradation, ...]:
    if fault.kind == "none" or fault.target is None:
        return ()
    onset = fault.start_utc + timedelta(seconds=ONSET_DELAY_S[fault.kind])
    callers: dict[str, list[str]] = {}
    out_edges: dict[str, list] = {}
    for e in edges:
        callers.setdefault(e.callee, []).append(e.caller)
        out_edges.setdefault(e.caller, []).append(e)
    depth = {fault.target: 0}
    q = deque([fault.target])
    while q:
        n = q.popleft()
        for c in sorted(callers.get(n, ())):
            if c not in depth:
                depth[c] = depth[n] + 1
                q.append(c)

    def at(d: int) -> datetime:
        return onset + timedelta(seconds=sum(HOP_LAG_S[:d]))

    out = [Degradation(app=AppId(fault.target), start_utc=onset, kind="latency", magnitude=2.0)]
    roots = []
    for app, d in sorted(depth.items(), key=lambda kv: (kv[1], kv[0])):
        if d == 0:
            continue
        is_root = not callers.get(app)
        kind, mag = _kind_at(app, out_edges, depth, is_root)
        out.append(Degradation(app=AppId(app), start_utc=at(d), kind=kind, magnitude=mag))
        if is_root:
            roots.append(app)
    # Gateways directly in front of the clients start shedding load afterwards.
    for root in roots:
        shed_at = at(depth[root]) + timedelta(seconds=LOAD_SHED_AFTER_CLIENTS_S)
        for e in out_edges[root]:
            if e.callee in depth and depth[e.callee] < depth[root]:
                out.append(Degradation(app=AppId(e.callee), start_utc=shed_at,
                                       kind="load_shed", magnitude=0.3))
    seen, res = set(), []
    for x in out:
        if (x.app, x.kind) not in seen and window.start_utc <= x.start_utc < window.end_utc:
            seen.add((x.app, x.kind))
            res.append(x)
    return tuple(res)
