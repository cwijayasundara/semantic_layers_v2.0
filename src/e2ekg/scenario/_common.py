"""Shared builders for hero and variant scenarios."""
from datetime import datetime, timedelta

import numpy as np

from e2ekg.scenario.model import AppId, Application, Change, ChainLink
from e2ekg.scenario.topology import downstream_closure, upstream_closure

ALERT_AFTER_CLIENTS = timedelta(minutes=10)
INCIDENT_AFTER_ALERT = timedelta(minutes=4)

_BG_KINDS = ("deployment", "config_change", "deployment", "config_change", "deployment")
_BG_ATTRS = {
    "deployment": lambda app, n: {"version": f"{app}-v{2 + n}.{n}.0", "strategy": "red-black"},
    "config_change": lambda app, n: {"property": f"{app}.timeout.ms", "old": "800", "new": "1000"},
}


def evidence_tags(chain: tuple[ChainLink, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(f"evidence:{link.signal}" for link in chain))


def background_changes(catalog: tuple[Application, ...], edges, rng: np.random.Generator,
                       avoid: set[str], times: list[datetime]) -> list[Change]:
    """Unrelated deployments/config changes on filler apps outside `avoid` and
    outside the closures of every avoided app. Never root cause, never a herring."""
    blocked = set(avoid)
    for a in avoid:
        blocked |= upstream_closure(edges, AppId(a)) | downstream_closure(edges, AppId(a))
    fillers = sorted(a.id for a in catalog if not a.aliases and a.id not in blocked)
    picks = [fillers[int(i)] for i in rng.permutation(len(fillers))[:len(times)]]
    res = []
    for n, (app, at) in enumerate(zip(picks, times), 1):
        kind = _BG_KINDS[(n - 1) % len(_BG_KINDS)]
        res.append(Change(id=f"chg-bg-{n:04d}", kind=kind, app=AppId(app), at_utc=at,
                          attrs=_BG_ATTRS[kind](app, n), is_root_cause=False))
    return res
