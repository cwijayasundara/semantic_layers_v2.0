from datetime import datetime
from typing import Literal, NewType

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

AppId = NewType("AppId", str)
AppKind = Literal["client", "edge", "service", "datastore", "cache"]
Protocol = Literal["grpc", "http", "graphql", "cql", "memcached"]
FaultKind = Literal[
    "capacity_reduction", "bad_deploy", "cache_eviction_storm",
    "db_hot_partition", "cert_expiry", "retry_storm", "none",
]
ChangeKind = Literal[
    "deployment", "config_change", "scaling",
    "experiment_allocation", "commit", "client_release",
]

SIGNAL_TYPES = (
    "log_error", "metric_anomaly", "slow_trace", "failed_trace", "alert",
    "incident", "deployment", "config_change", "experiment", "autoscaling",
    "commit", "e2e_bottleneck", "error_pattern",
)
CHANGE_SIGNAL = {
    "deployment": "deployment",
    "config_change": "config_change",
    "scaling": "autoscaling",
    "experiment_allocation": "experiment",
    "commit": "commit",
    "client_release": "deployment",
}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Application(_Frozen):
    id: AppId
    aliases: tuple[str, ...]
    owner_team: str
    tier: int
    regions: tuple[str, ...]
    kind: AppKind


class DependencyEdge(_Frozen):
    caller: AppId
    callee: AppId
    protocol: Protocol
    pool_size: int | None


class Fault(_Frozen):
    kind: FaultKind
    target: AppId | None
    start_utc: datetime
    trigger_change_id: str | None
    fix_change_id: str | None = None


class Change(_Frozen):
    id: str
    kind: ChangeKind
    app: AppId
    at_utc: datetime
    attrs: dict[str, str]
    is_root_cause: bool


class Window(_Frozen):
    start_utc: datetime
    end_utc: datetime
    metric_step_s: int


class ChainLink(_Frozen):
    signal: str
    app: AppId
    at_utc: datetime

    @field_validator("signal")
    @classmethod
    def _known_signal(cls, v: str) -> str:
        if v not in SIGNAL_TYPES:
            raise ValueError(f"unknown signal type {v!r}")
        return v


class AnswerKey(_Frozen):
    root_cause_change_id: str | None
    causal_chain: tuple[ChainLink, ...]
    red_herring_change_ids: tuple[str, ...]
    evidence_tags: tuple[str, ...]
    fix_change_id: str | None = None
    recovered_utc: datetime | None = None


class PastIncident(_Frozen):
    id: str
    opened_utc: datetime
    severity: Literal["SEV1", "SEV2", "SEV3"]
    affected: AppId
    cause_change_kind: ChangeKind | None
    cause_app: AppId | None
    resolver_handle: str
    summary: str


class Scenario(_Frozen):
    name: str
    seed: int
    catalog: tuple[Application, ...]
    edges: tuple[DependencyEdge, ...]
    window: Window
    fault: Fault
    changes: tuple[Change, ...]
    history: tuple[PastIncident, ...]
    answer_key: AnswerKey

    @model_validator(mode="after")
    def _apps_exist(self) -> "Scenario":
        known = {a.id for a in self.catalog}
        refs = [x for e in self.edges for x in (e.caller, e.callee)]
        refs += [c.app for c in self.changes]
        refs += [self.fault.target] if self.fault.target is not None else []
        refs += [link.app for link in self.answer_key.causal_chain]
        for h in self.history:
            refs.append(h.affected)
            if h.cause_app is not None:
                refs.append(h.cause_app)
        for ref in refs:
            if ref not in known:
                raise ValueError(f"unknown app {ref!r}")
        change_ids = {c.id for c in self.changes}
        cids = [self.fault.trigger_change_id, self.answer_key.root_cause_change_id,
                self.fault.fix_change_id, self.answer_key.fix_change_id]
        cids += list(self.answer_key.red_herring_change_ids)
        for cid in cids:
            if cid is not None and cid not in change_ids:
                raise ValueError(f"unknown change {cid!r}")
        return self
