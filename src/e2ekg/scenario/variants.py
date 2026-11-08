"""Six variant scenarios over the hero world: five faults plus a no-fault control.

Each variant has its own date (2026-10-1x) and fault time of day. The window is
[floor_hour(fault) - 2h, +6h), mirroring the hero (14:52 -> 12:00-18:00).
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from e2ekg.scenario._common import ALERT_AFTER_CLIENTS, INCIDENT_AFTER_ALERT, background_changes, evidence_tags
from e2ekg.scenario.history import build_history
from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.model import (
    CHANGE_SIGNAL, AnswerKey, Change, ChainLink, Fault, FaultKind, Scenario, Window,
)
from e2ekg.scenario.propagation import propagate
from e2ekg.timeutil import PT, pt

_BG_OFFSETS_MIN = (-105, -70, -40, -14, -6, 12, 45, 90)
_FIX_AFTER_MIN = 150
_DEG_SIGNAL = {"pool_exhaustion": "log_error", "timeouts": "slow_trace",
               "partial_response": "slow_trace"}


@dataclass(frozen=True)
class _Spec:
    name: str
    day: int
    hhmm: tuple[int, int]
    kind: FaultKind
    target: str | None
    # (id, kind, app, offset_min_from_fault_start, attrs, is_root_cause)
    changes: tuple
    chain_ids: tuple[str, ...]  # changes that open the causal chain, in order
    herrings: tuple[str, ...]
    root: str | None
    avoid: tuple[str, ...] = field(default=())
    # The real fix, after the cutoff: (id, kind, app, offset_min_from_fault_start, attrs)
    fix: tuple | None = None


_SPECS = (
    _Spec("v-bad-deploy", 8, (10, 30), "bad_deploy", "lolomo-svc", (
        ("chg-dep-0001", "deployment", "lolomo-svc", 0, {"version": "lolomo-svc-v5.88.1", "strategy": "red-black"}, True),
        ("chg-exp-0001", "experiment_allocation", "ab-allocator", 3, {"experiment": "billboard-cell-c", "old_allocation_pct": "0", "new_allocation_pct": "10"}, False),
        ("chg-rel-0001", "client_release", "client-ios", 6, {"version": "15.4.0", "channel": "production", "rollout_pct": "50"}, False),
    ), ("chg-dep-0001",), ("chg-exp-0001", "chg-rel-0001"), "chg-dep-0001", ("ab-allocator", "client-ios"),
       ("chg-dep-0002", "deployment", "lolomo-svc", _FIX_AFTER_MIN, {"version": "lolomo-svc-v5.88.0", "strategy": "rollback"})),
    _Spec("v-cache-storm", 9, (13, 41), "cache_eviction_storm", "evcache-titles", (
        ("chg-cfg-0001", "config_change", "evcache-titles", 0, {"property": "evcache-titles.ttl.seconds", "old": "86400", "new": "60"}, True),
        ("chg-dep-0001", "deployment", "title-metadata-svc", 5, {"version": "title-metadata-svc-v9.2.4", "strategy": "red-black"}, False),
        ("chg-exp-0001", "experiment_allocation", "row-ranker", 8, {"experiment": "row-ranker-cell-d", "old_allocation_pct": "5", "new_allocation_pct": "15"}, False),
    ), ("chg-cfg-0001",), ("chg-dep-0001", "chg-exp-0001"), "chg-cfg-0001", ("title-metadata-svc", "row-ranker"),
       ("chg-cfg-0002", "config_change", "evcache-titles", _FIX_AFTER_MIN, {"property": "evcache-titles.ttl.seconds", "old": "60", "new": "86400"})),
    _Spec("v-hot-partition", 10, (9, 17), "db_hot_partition", "cassandra-titles", (
        ("chg-cfg-0001", "config_change", "cassandra-titles", 0, {"property": "cassandra-titles.titles.partition_key", "old": "titleId", "new": "region"}, True),
        ("chg-dep-0001", "deployment", "search-svc", 4, {"version": "search-svc-v3.40.0", "strategy": "red-black"}, False),
        ("chg-rel-0001", "client_release", "client-web", 7, {"version": "web-2026.41", "channel": "production", "rollout_pct": "100"}, False),
    ), ("chg-cfg-0001",), ("chg-dep-0001", "chg-rel-0001"), "chg-cfg-0001", ("search-svc", "client-web"),
       ("chg-cfg-0002", "config_change", "cassandra-titles", _FIX_AFTER_MIN, {"property": "cassandra-titles.titles.partition_key", "old": "region", "new": "titleId"})),
    _Spec("v-cert-expiry", 11, (16, 5), "cert_expiry", "entitlement-svc", (
        ("chg-cfg-0001", "config_change", "entitlement-svc", 0, {"property": "entitlement-svc.tls.cert.serial", "old": "0xA41C", "new": "0xB77E", "note": "cert rotation"}, True),
        ("chg-dep-0001", "deployment", "api-gateway", 5, {"version": "api-gateway-v7.32.0", "strategy": "red-black"}, False),
        ("chg-exp-0001", "experiment_allocation", "ab-allocator", 9, {"experiment": "paywall-cell-b", "old_allocation_pct": "0", "new_allocation_pct": "5"}, False),
    ), ("chg-cfg-0001",), ("chg-dep-0001", "chg-exp-0001"), "chg-cfg-0001", ("api-gateway", "ab-allocator"),
       ("chg-cfg-0002", "config_change", "entitlement-svc", _FIX_AFTER_MIN, {"property": "entitlement-svc.tls.cert.serial", "old": "0xB77E", "new": "0xC9D2", "note": "re-issued cert"})),
    _Spec("v-retry-storm", 12, (11, 8), "retry_storm", "row-ranker", (
        ("chg-cmt-0001", "commit", "row-ranker", -8, {"sha": "c0ffee1", "message": "add retries (max 5, no backoff) to title-batch-loader calls"}, False),
        ("chg-dep-0001", "deployment", "row-ranker", 0, {"version": "row-ranker-v12.7.0", "strategy": "red-black"}, True),
        ("chg-cfg-0001", "config_change", "profile-svc", 4, {"property": "profile-svc.cache.size", "old": "5000", "new": "8000"}, False),
        ("chg-rel-0001", "client_release", "client-android", 8, {"version": "8.113.0", "channel": "production", "rollout_pct": "100"}, False),
    ), ("chg-cmt-0001", "chg-dep-0001"), ("chg-cfg-0001", "chg-rel-0001"), "chg-dep-0001", ("profile-svc", "client-android"),
       ("chg-dep-0002", "deployment", "row-ranker", _FIX_AFTER_MIN, {"version": "row-ranker-v12.6.3", "strategy": "rollback"})),
    _Spec("v-control", 13, (14, 30), "none", None, (
        ("chg-dep-0001", "deployment", "api-gateway", 0, {"version": "api-gateway-v7.33.0", "strategy": "red-black"}, False),
        ("chg-exp-0001", "experiment_allocation", "row-ranker", 5, {"experiment": "row-ranker-cell-e", "old_allocation_pct": "0", "new_allocation_pct": "5"}, False),
        ("chg-cfg-0001", "config_change", "title-metadata-svc", 9, {"property": "title-metadata-svc.asg.minInstances", "old": "48", "new": "44"}, False),
    ), (), ("chg-dep-0001", "chg-exp-0001", "chg-cfg-0001"), None, ("api-gateway", "row-ranker", "title-metadata-svc")),
)


def _chain(spec: _Spec, fault: Fault, changes: dict[str, Change], degs) -> tuple[ChainLink, ...]:
    links = [ChainLink(signal=CHANGE_SIGNAL[changes[c].kind], app=changes[c].app, at_utc=changes[c].at_utc)
             for c in spec.chain_ids]
    links.append(ChainLink(signal="metric_anomaly", app=fault.target, at_utc=degs[0].start_utc))
    clients, last_svc = [], None
    for d in degs[1:]:
        if d.kind == "load_shed":
            continue
        if d.kind == "latency":  # client-side TTR; one representative client
            clients.append(d)
            continue
        links.append(ChainLink(signal=_DEG_SIGNAL[d.kind], app=d.app, at_utc=d.start_utc))
        last_svc = d.app
    first_client = min(clients, key=lambda d: (d.start_utc, d.app))
    links.append(ChainLink(signal="metric_anomaly", app=first_client.app, at_utc=first_client.start_utc))
    alert_at = first_client.start_utc + ALERT_AFTER_CLIENTS
    links.append(ChainLink(signal="alert", app=last_svc, at_utc=alert_at))
    links.append(ChainLink(signal="incident", app=last_svc, at_utc=alert_at + INCIDENT_AFTER_ALERT))
    return tuple(links)


def variant_scenarios(seed: int = 7) -> tuple[Scenario, ...]:
    world = hero_scenario(seed)
    out = []
    for idx, spec in enumerate(_SPECS):
        start = pt(2026, 10, spec.day, *spec.hhmm)
        floor = start.astimezone(PT).replace(minute=0, second=0, microsecond=0)
        w0 = floor - timedelta(hours=2)
        window = Window(start_utc=w0, end_utc=w0 + timedelta(hours=6), metric_step_s=60)
        changes = [Change(id=i, kind=k, app=a, at_utc=start + timedelta(minutes=off), attrs=at,
                          is_root_cause=rc) for i, k, a, off, at, rc in spec.changes]
        avoid = set(spec.avoid) | ({spec.target} if spec.target else set())
        avoid |= {c.app for c in changes}
        changes += background_changes(world.catalog, world.edges, np.random.default_rng(seed + 10 + idx),
                                      avoid, [start + timedelta(minutes=m) for m in _BG_OFFSETS_MIN])
        fix = None
        if spec.fix is not None:
            i, k, a, off, at = spec.fix
            fix = Change(id=i, kind=k, app=a, at_utc=start + timedelta(minutes=off), attrs=at,
                         is_root_cause=False)
            changes.append(fix)
        changes.sort(key=lambda c: (c.at_utc, c.id))
        trigger = spec.root
        fault = Fault(kind=spec.kind, target=spec.target, start_utc=start, trigger_change_id=trigger,
                      fix_change_id=fix.id if fix else None)
        by_id = {c.id: c for c in changes}
        if spec.kind == "none":
            chain: tuple[ChainLink, ...] = ()
        else:
            chain = _chain(spec, fault, by_id, propagate(fault, world.edges, window))
        recovered = None
        if fix is not None:
            recovered = max(d.end_utc for d in propagate(fault, world.edges, window,
                                                         fixed_utc=fix.at_utc))
        key = AnswerKey(root_cause_change_id=spec.root, causal_chain=chain,
                        red_herring_change_ids=spec.herrings, evidence_tags=evidence_tags(chain),
                        fix_change_id=fault.fix_change_id, recovered_utc=recovered)
        history = build_history(world.catalog, world.edges, np.random.default_rng(seed + 100 + idx),
                                90, window.start_utc)
        out.append(Scenario(name=spec.name, seed=seed, catalog=world.catalog, edges=world.edges,
                            window=window, fault=fault, changes=tuple(changes), history=history,
                            answer_key=key))
    return tuple(out)
