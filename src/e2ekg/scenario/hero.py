"""The hero incident from the Netflix post (2026-10-14, America/Los_Angeles)."""
import numpy as np

from e2ekg.scenario._common import ALERT_AFTER_CLIENTS, INCIDENT_AFTER_ALERT, background_changes, evidence_tags
from e2ekg.scenario.history import build_history
from e2ekg.scenario.model import AnswerKey, Change, ChainLink, Fault, Scenario, Window
from e2ekg.scenario.propagation import propagate
from e2ekg.scenario.topology import hero_catalog, hero_edges
from e2ekg.timeutil import pt


def _t(h: int, m: int, s: int = 0):
    return pt(2026, 10, 14, h, m, s)


_DESIRED = (48, 40, 32, 24, 16, 12)
_PREVIOUS = (56, 48, 40, 32, 24, 16)
_BG_TIMES = [(12, 40), (13, 25), (14, 5), (15, 8), (15, 14), (15, 17), (16, 10), (17, 2)]


def hero_scenario(seed: int = 7) -> Scenario:
    catalog = hero_catalog(np.random.default_rng(seed))
    edges = hero_edges(catalog)
    window = Window(start_utc=_t(12, 0), end_utc=_t(18, 0), metric_step_s=60)
    fault = Fault(kind="capacity_reduction", target="title-metadata-svc",
                  start_utc=_t(14, 52), trigger_change_id="chg-cfg-0001")

    changes = [Change(
        id="chg-cfg-0001", kind="config_change", app="title-metadata-svc", at_utc=_t(14, 52),
        attrs={"property": "title-metadata-svc.asg.minInstances", "old": "48", "new": "12"},
        is_root_cause=True)]
    for i, (des, prev) in enumerate(zip(_DESIRED, _PREVIOUS)):
        changes.append(Change(
            id=f"chg-asg-{i + 1:04d}", kind="scaling", app="title-metadata-svc",
            at_utc=_t(15, 2 * i),
            attrs={"asg": "title-metadata-svc-v042", "previous_capacity": str(prev),
                   "desired_capacity": str(des)}, is_root_cause=False))
    changes += [
        Change(id="chg-exp-0001", kind="experiment_allocation", app="row-ranker", at_utc=_t(15, 5),
               attrs={"experiment": "row-ranker-cell-b", "cell": "B", "old_allocation_pct": "0",
                      "new_allocation_pct": "5", "population": "members"}, is_root_cause=False),
        Change(id="chg-rel-0001", kind="client_release", app="client-android", at_utc=_t(15, 12),
               attrs={"version": "8.112.0", "channel": "production", "rollout_pct": "100"},
               is_root_cause=False),
        Change(id="chg-dep-0001", kind="deployment", app="api-gateway", at_utc=_t(15, 18),
               attrs={"version": "api-gateway-v7.31.2", "strategy": "red-black"}, is_root_cause=False),
    ]
    chain_apps = {"title-metadata-svc", "title-batch-loader", "lolomo-svc", "client-tv",
                  "homepage-graphql", "row-ranker", "client-android", "api-gateway"}
    changes += background_changes(catalog, edges, np.random.default_rng(seed + 2), chain_apps,
                                  [_t(h, m) for h, m in _BG_TIMES])
    changes.sort(key=lambda c: (c.at_utc, c.id))

    deg = {(d.app, d.kind): d.start_utc for d in propagate(fault, edges, window)}
    first_scale = next(c for c in changes if c.id == "chg-asg-0001").at_utc
    client_at = deg[("client-tv", "latency")]
    alert_at = client_at + ALERT_AFTER_CLIENTS
    chain = (
        ChainLink(signal="config_change", app="title-metadata-svc", at_utc=fault.start_utc),
        ChainLink(signal="autoscaling", app="title-metadata-svc", at_utc=first_scale),
        ChainLink(signal="metric_anomaly", app="title-metadata-svc",
                  at_utc=deg[("title-metadata-svc", "latency")]),
        ChainLink(signal="log_error", app="title-batch-loader",
                  at_utc=deg[("title-batch-loader", "pool_exhaustion")]),
        ChainLink(signal="slow_trace", app="lolomo-svc", at_utc=deg[("lolomo-svc", "timeouts")]),
        ChainLink(signal="metric_anomaly", app="client-tv", at_utc=client_at),
        ChainLink(signal="alert", app="homepage-graphql", at_utc=alert_at),
        ChainLink(signal="incident", app="homepage-graphql", at_utc=alert_at + INCIDENT_AFTER_ALERT),
    )
    key = AnswerKey(root_cause_change_id="chg-cfg-0001", causal_chain=chain,
                    red_herring_change_ids=("chg-exp-0001", "chg-rel-0001", "chg-dep-0001"),
                    evidence_tags=evidence_tags(chain))
    history = build_history(catalog, edges, np.random.default_rng(seed + 1), 90, window.start_utc)
    return Scenario(name="hero", seed=seed, catalog=catalog, edges=edges, window=window,
                    fault=fault, changes=tuple(changes), history=history, answer_key=key)
