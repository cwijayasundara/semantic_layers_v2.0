"""Synthetic 90-day incident history: a misleading api-gateway prior plus one
'last quarter' twin of the hero incident. Deterministic given rng."""
from datetime import datetime, timedelta

import numpy as np

from e2ekg.scenario.model import AppId, Application, ChangeKind, DependencyEdge, PastIncident
from e2ekg.scenario.topology import upstream_closure

TWIN_RESOLVER = "priya.nair"  # reused by the Slack renderer in the hero channel
TWIN_DAYS_BEFORE = 95  # +/- 2 days; outside the general `days` span on purpose

N_TOTAL, N_SEV2, N_GATEWAY, N_TIMEOUT = 30, 25, 10, 3

_HANDLES = ("a.kowalski", "j.mbeki", "li.wei", "r.santos", "t.fischer", "m.haddad",
            "s.oyelaran", "k.tanaka", "n.dubois", "o.lindqvist", "v.rao", "e.moreau")
_GW_AFFECTED = ("playback-api", "entitlement-svc", "profile-svc", "edge-gateway")
_OTHER_CAUSES: tuple[tuple[str, ChangeKind | None], ...] = (
    ("row-ranker", "deployment"), ("lolomo-svc", "deployment"),
    ("evcache-titles", "config_change"), ("cassandra-titles", "scaling"),
    ("entitlement-svc", "config_change"), ("artwork-svc", "deployment"),
    ("ab-allocator", "experiment_allocation"), ("profile-svc", "deployment"),
    ("search-svc", "commit"), ("homepage-graphql", "deployment"),
    ("title-batch-loader", "deployment"), ("ratings-svc", None),
    ("client-android", "client_release"), ("title-metadata-svc", "deployment"),
)


def build_history(catalog: tuple[Application, ...], edges: tuple[DependencyEdge, ...],
                  rng: np.random.Generator, days: int = 90,
                  end_utc: datetime | None = None) -> tuple[PastIncident, ...]:
    if end_utc is None:
        raise ValueError("end_utc is required")
    known = {a.id for a in catalog}

    def opened() -> datetime:
        return end_utc - timedelta(minutes=int(rng.integers(60, days * 24 * 60)))

    def handle() -> str:
        return _HANDLES[int(rng.integers(len(_HANDLES)))]

    out: list[PastIncident] = []
    # 10 api-gateway deployment SEV2s; exactly 3 mention Connection Timeout.
    timeout_idx = set(int(i) for i in rng.permutation(N_GATEWAY)[:N_TIMEOUT])
    for n in range(N_GATEWAY):
        aff = _GW_AFFECTED[int(rng.integers(len(_GW_AFFECTED)))]
        if n in timeout_idx:
            summ = f"Connection Timeout errors on {aff} after api-gateway deployment; rolled back"
        else:
            summ = f"Elevated 5xx on {aff} after api-gateway deployment; rolled back"
        out.append(PastIncident(
            id="", opened_utc=opened(), severity="SEV2", affected=AppId(aff),
            cause_change_kind="deployment", cause_app=AppId("api-gateway"),
            resolver_handle=handle(), summary=summ))
    # 14 other SEV2s and 5 SEV1/SEV3 with varied causes (never gateway, never the twin pattern).
    sevs = ["SEV2"] * (N_SEV2 - N_GATEWAY - 1) + ["SEV1", "SEV1", "SEV3", "SEV3", "SEV3"]
    for n, sev in enumerate(sevs):
        cause_app, kind = _OTHER_CAUSES[(n + int(rng.integers(len(_OTHER_CAUSES)))) % len(_OTHER_CAUSES)]
        if cause_app == "title-metadata-svc" and kind == "config_change":
            kind = "deployment"
        ups = sorted(upstream_closure(edges, AppId(cause_app)) | {AppId(cause_app)})
        aff = ups[int(rng.integers(len(ups)))]
        what = f"{kind.replace('_', ' ')} on {cause_app}" if kind else "no identified change"
        out.append(PastIncident(
            id="", opened_utc=opened(), severity=sev, affected=aff,  # type: ignore[arg-type]
            cause_change_kind=kind, cause_app=AppId(cause_app),
            resolver_handle=handle(),
            summary=f"{sev} degradation of {aff}: {what}; mitigated by reverting"))
    # The twin: last-quarter lolomo-svc incident from a title-metadata-svc capacity config change.
    twin_at = end_utc - timedelta(days=TWIN_DAYS_BEFORE + int(rng.integers(-2, 3)),
                                  minutes=int(rng.integers(0, 600)))
    out.append(PastIncident(
        id="", opened_utc=twin_at, severity="SEV2", affected=AppId("lolomo-svc"),
        cause_change_kind="config_change", cause_app=AppId("title-metadata-svc"),
        resolver_handle=TWIN_RESOLVER,
        summary="Home rows empty on lolomo-svc after title-metadata-svc minInstances config change "
                "cut capacity; pool exhaustion in title-batch-loader; config reverted"))
    out.sort(key=lambda i: i.opened_utc)
    res = tuple(i.model_copy(update={"id": f"inc-hist-{k:03d}"}) for k, i in enumerate(out, 1))
    assert all(i.affected in known and (i.cause_app in known) for i in res)
    return res
