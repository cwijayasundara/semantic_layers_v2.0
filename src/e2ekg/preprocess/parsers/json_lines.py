"""JSON Lines: one JSON object per line, one parser class configured per
dialect (JSON logs, Atlas points, Spinnaker / ASG / Radar / PagerDuty /
incident.io events). A dialect lists the keys its source schema defines;
anything else goes to `_residual`. Key lists describe the source systems'
export schemas, not the generator."""
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from e2ekg.preprocess.parsers._base import (
    Unparseable,
    first_str,
    load_json,
    row,
    split_known,
    split_lines,
    ts_str,
)


@dataclass(frozen=True)
class JsonLinesParser:
    name: str
    source: str
    known: frozenset[str]
    ts_key: str | None = "ts"
    app_keys: tuple[str, ...] = ("app",)
    cols: Callable[[dict], dict] = field(default=lambda d: {})

    def split(self, data: bytes) -> Iterator[tuple[int, int]]:
        return split_lines(data)

    def parse(self, payload: bytes) -> dict:
        d = load_json(payload)
        if not isinstance(d, dict):
            raise Unparseable(f"{self.name}: expected a JSON object")
        fields, residual = split_known(d, self.known)
        return row(ts_str(d.get(self.ts_key)) if self.ts_key else None,
                   first_str(d, self.app_keys), fields, residual, **self.cols(d))


def _s(v) -> str | None:
    return None if v is None else str(v)


def _log_cols(d: dict) -> dict:
    level = d.get("level")
    return {"level": level.upper() if isinstance(level, str) else None,
            "logger": _s(d.get("logger")), "thread": _s(d.get("thread")),
            "msg": _s(d.get("msg")), "request_id": _s(d.get("request_id"))}


def _atlas_cols(d: dict) -> dict:
    v = d.get("v")
    return {"metric": _s(d.get("name")), "tags": d.get("tags") or {},
            "value": float(v) if isinstance(v, (int, float)) and not isinstance(v, bool)
            else None}


def _event_cols(kind: str, id_key: str, type_key: str | None) -> Callable[[dict], dict]:
    return lambda d: {"event_kind": kind, "row_kind": "record", "external_id": _s(d.get(id_key)),
                      "event_type": _s(d.get(type_key)) if type_key else None,
                      "status": _s(d.get("status"))}


JSON_LOG = JsonLinesParser(
    "json_lines", "logs",
    frozenset({"ts", "level", "app", "host", "logger", "thread", "msg", "request_id",
               "stack_trace"}), cols=_log_cols)

ATLAS = JsonLinesParser(
    "atlas", "atlas", frozenset({"name", "tags", "t", "v"}), ts_key="t", app_keys=(),
    cols=_atlas_cols)

SPINNAKER = JsonLinesParser(
    "spinnaker", "events",
    frozenset({"id", "type", "app", "cluster", "pipeline", "version", "strategy", "status", "ts",
               "triggered_by", "description", "channel", "rollout_pct"}),
    app_keys=("app", "cluster"), cols=_event_cols("spinnaker", "id", "type"))

ASG = JsonLinesParser(
    "asg", "events",
    frozenset({"ts", "app", "asg", "activity_id", "previous_capacity", "desired_capacity",
               "status", "cause", "description", "user"}),
    app_keys=("app", "asg"), cols=_event_cols("asg", "activity_id", None))

RADAR = JsonLinesParser(
    "radar", "events",
    frozenset({"alert_id", "ts", "app", "service", "severity", "metric", "state", "summary",
               "description"}),
    app_keys=("service", "app"),
    cols=lambda d: {"alert_id": _s(d.get("alert_id")), "severity": _s(d.get("severity")),
                    "state": _s(d.get("state")), "metric": _s(d.get("metric"))})

PAGERDUTY = JsonLinesParser(
    "pagerduty", "events",
    frozenset({"id", "ts", "app", "service", "urgency", "title", "status", "assignee",
               "description", "incident_key"}),
    app_keys=("service", "app"),
    cols=lambda d: {"page_id": _s(d.get("id")), "status": _s(d.get("status")),
                    "urgency": _s(d.get("urgency")), "title": _s(d.get("title"))})


def _incident_cols(d: dict) -> dict:
    return {"incident_id": _s(d.get("id")), "event": _s(d.get("event")),
            "status": _s(d.get("status")), "severity": _s(d.get("severity"))}


class _IncidentParser(JsonLinesParser):
    """incident.io names no single app; the first affected service is the
    surface form (the list itself stays in `fields`)."""

    def parse(self, payload: bytes) -> dict:
        r = super().parse(payload)
        svcs = r["fields"].get("affected_services")
        if r["surface_app"] is None and isinstance(svcs, list) and svcs:
            r["surface_app"] = _s(svcs[0])
        return r


INCIDENT = _IncidentParser(
    "incident", "events",
    frozenset({"id", "ts", "app", "service", "severity", "slack_channel", "event", "name",
               "status", "affected_services", "summary", "resolved_by", "postmortem",
               "description", "incident_lead"}),
    app_keys=("app", "service"), cols=_incident_cols)
