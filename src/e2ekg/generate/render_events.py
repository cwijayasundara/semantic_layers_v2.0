"""Change events, alerts, pages and incidents.

events/spinnaker.jsonl (deployments, client releases), events/properties.log
(fast-property config changes), events/asg.jsonl (scaling activities),
events/ab_allocations.csv, events/commits.log (git log), alerts/radar.jsonl,
pagerduty/pages.jsonl and incidents/incident_io.jsonl (R13 names).

Every scenario change carries its id as `change_id` (Spinnaker, properties,
ASG), `allocation_id` (A/B csv) or a `Change-Id:` trailer (commits). The live
incident's first `affected_services` is a wrong early guess (the latest
red-herring deployment's app), corrected 40 min later. History incidents get
incident records and pages at their own historical times."""
import hashlib
from datetime import timedelta

import numpy as np

from e2ekg.generate.people import (
    HISTORY_HANDLES,
    history_channel,
    history_numbers,
    live_channel,
    live_incident_number,
    oncall,
)
from e2ekg.generate.records import RawRecord
from e2ekg.generate.world import World, tags
from e2ekg.scenario.model import Change, Scenario
from e2ekg.scenario.propagation import Degradation

CORRECTION_AFTER = timedelta(minutes=40)
DEPLOY_DURATION = timedelta(minutes=4)
AUTHORS = ("Dana Lee <dlee@netflix.com>", "Marco Rossi <mrossi@netflix.com>",
           "Aisha Khan <akhan@netflix.com>", "Tomas Novak <tnovak@netflix.com>",
           "Yuki Sato <ysato@netflix.com>", "Ines Costa <icosta@netflix.com>")
DEPLOY_COMMITS = ("Bump grpc-java to 1.68.1", "Add hedged-request metrics",
                  "Fix flaky readiness probe", "Tune HTTP/2 keepalive to 30s",
                  "Move to Java 21 base image", "Refactor filter chain ordering",
                  "Log upstream status on fallback", "Remove dead feature flag")
NOISE_ALERTS = (("CPU utilization above 80% for 5m", "warning", "sys.cpu.utilization"),
                ("GC pause p99 above 500ms", "warning", "jvm.gc.pause.p99"),
                ("Disk usage above 85%", "warning", "sys.disk.used.pct"),
                ("Error rate above baseline", "warning", "server.errors.rps"))
N_NOISE_ALERTS = 10


def _rec(rel: str, fmt: str, fields: dict, tg=frozenset()) -> RawRecord:
    return RawRecord(source="events", rel_path=rel, fmt=fmt, fields=fields,
                     at_utc=fields.get("ts", fields.get("date")), tags=tg)


def _hexid(*parts: object, n: int = 40) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:n]


def _deploy(w: World, c: Change, rng) -> list[RawRecord]:
    tg = w.change_tags(c)
    exec_id = "01J" + _hexid("exec", c.id, n=23).upper()
    who = HISTORY_HANDLES[int(rng.integers(len(HISTORY_HANDLES)))]
    base = {"id": exec_id, "change_id": c.id}
    if c.kind == "client_release":
        return [_rec("events/spinnaker.jsonl", "spinnaker_jsonl", {
            **base, "type": "client_release", "app": c.app, "pipeline": "Play Store promote",
            "version": c.attrs.get("version"), "channel": c.attrs.get("channel"),
            "rollout_pct": c.attrs.get("rollout_pct"), "status": "SUCCEEDED", "ts": c.at_utc,
            "triggered_by": who,
            "description": f"Promote {c.app} {c.attrs.get('version')} to "
                           f"{c.attrs.get('rollout_pct')}% of {c.attrs.get('channel')}"}, tg)]
    out = []
    for status, at in (("RUNNING", c.at_utc - DEPLOY_DURATION), ("SUCCEEDED", c.at_utc)):
        out.append(_rec("events/spinnaker.jsonl", "spinnaker_jsonl", {
            **base, "type": "deploy", "app": c.app, "pipeline": "Deploy to prod",
            "version": c.attrs.get("version"), "strategy": c.attrs.get("strategy"),
            "status": status, "ts": at, "triggered_by": who,
            "description": f"{c.attrs.get('strategy', 'red-black')} deploy of "
                           f"{c.attrs.get('version')} ({status.lower()})"}, tg))
    return out


def _config(w: World, c: Change, rng) -> RawRecord:
    extra = {k: v for k, v in c.attrs.items() if k not in ("property", "old", "new")}
    return _rec("events/properties.log", "properties_log", {
        "ts": c.at_utc, "app": c.app, "property": c.attrs["property"], "old": c.attrs.get("old"),
        "new": c.attrs.get("new"), "user": HISTORY_HANDLES[int(rng.integers(len(HISTORY_HANDLES)))],
        "source": "fastprops-ui", "change_id": c.id, **extra}, w.change_tags(c))


def _scaling(w: World, c: Change) -> RawRecord:
    prev, des = c.attrs["previous_capacity"], c.attrs["desired_capacity"]
    stamp = c.at_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    return _rec("events/asg.jsonl", "asg_jsonl", {
        "ts": c.at_utc, "app": c.app, "asg": c.attrs.get("asg"),
        "activity_id": _hexid("asg", c.id, n=8) + "-" + _hexid("asg2", c.id, n=4),
        "change_id": c.id, "previous_capacity": int(prev), "desired_capacity": int(des),
        "status": "Successful",
        "cause": f"At {stamp} a monitor alarm TargetTracking-{c.attrs.get('asg')}-AlarmLow "
                 f"in state ALARM triggered policy cpu-target-tracking changing the desired "
                 f"capacity from {prev} to {des}.",
        "description": f"Terminating {int(prev) - int(des)} EC2 instances"}, w.change_tags(c))


def _ab(w: World, c: Change, rng) -> RawRecord:
    exp = c.attrs["experiment"]
    return _rec("events/ab_allocations.csv", "ab_csv", {
        "ts": c.at_utc, "experiment": exp,
        "cell": c.attrs.get("cell", exp.rsplit("-", 1)[-1].upper()),
        "old_allocation_pct": c.attrs.get("old_allocation_pct"),
        "new_allocation_pct": c.attrs.get("new_allocation_pct"),
        "population": c.attrs.get("population", "members"), "app": c.app,
        "actor": HISTORY_HANDLES[int(rng.integers(len(HISTORY_HANDLES)))],
        "allocation_id": c.id}, w.change_tags(c))


def _commits(w: World, rng) -> list[RawRecord]:
    out = []
    for c in w.scenario.changes:
        if c.kind == "commit":
            sha = (c.attrs.get("sha", "") + _hexid("sha", c.id))[:40]
            out.append(_rec("events/commits.log", "gitlog", {
                "sha": sha, "author": AUTHORS[int(rng.integers(len(AUTHORS)))], "date": c.at_utc,
                "message": f"{c.attrs.get('message', 'update')}\n\nChange-Id: {c.id}"},
                w.change_tags(c)))
        elif c.kind == "deployment":  # the code a deployment ships, landed earlier
            for k in range(1 + int(rng.integers(2))):
                at = c.at_utc - timedelta(minutes=int(rng.integers(25, 140)))
                out.append(_rec("events/commits.log", "gitlog", {
                    "sha": _hexid("sha", c.id, k),
                    "author": AUTHORS[int(rng.integers(len(AUTHORS)))],
                    "date": at, "message": f"[{c.app}] "
                    f"{DEPLOY_COMMITS[int(rng.integers(len(DEPLOY_COMMITS)))]}"}))
    return sorted(out, key=lambda r: r.at_utc)


def _alert(n: int, at, app, severity, metric, state, summary, tg=frozenset()) -> RawRecord:
    return RawRecord(source="events", rel_path="alerts/radar.jsonl", fmt="radar_jsonl", at_utc=at,
                     tags=tg, fields={"alert_id": f"radar-{n:05d}", "ts": at, "app": app,
                                      "severity": severity, "metric": metric, "state": state,
                                      "summary": summary})


def _page(pid: str, at, app, urgency, title, status, who, tg=frozenset(), key=None) -> RawRecord:
    f = {"id": pid, "ts": at, "app": app, "urgency": urgency, "title": title, "status": status,
         "assignee": who}
    if key:
        f["incident_key"] = key
    return RawRecord(source="events", rel_path="pagerduty/pages.jsonl", fmt="pagerduty_jsonl",
                     at_utc=at, tags=tg, fields=f)


def _live(w: World, rng) -> tuple[list[RawRecord], list[RawRecord], list[RawRecord]]:
    s = w.scenario
    alerts, pages, incs = [], [], []
    alert_links, inc_links = w.links("alert"), w.links("incident")
    lead = oncall(s)
    for k, (app, at) in enumerate(sorted(alert_links.items(), key=lambda kv: kv[1])):
        aid = 9000 + k
        alerts.append(_alert(aid, at, app, "critical", "client.ttr.p90", "firing",
                             f"TTR p90 SLO burn rate 14.4x on {app} (all platforms)",
                             tags("alert")))
        pid = "P" + _hexid("page", s.name, aid, n=6).upper()
        pages.append(_page(pid, at + timedelta(seconds=30), app, "high",
                           f"[CRITICAL] TTR p90 SLO burn on {app}", "triggered", lead,
                           tags("alert"), key=f"radar-{aid:05d}"))
        pages.append(_page(pid, at + timedelta(minutes=3), app, "high",
                           f"[CRITICAL] TTR p90 SLO burn on {app}", "acknowledged", lead,
                           key=f"radar-{aid:05d}"))
    by_id = {c.id: c for c in s.changes}
    herring_deploys = sorted((by_id[h] for h in s.answer_key.red_herring_change_ids
                              if by_id[h].kind == "deployment"), key=lambda c: c.at_utc)
    for app, at in inc_links.items():
        guess = herring_deploys[-1].app if herring_deploys else app
        num = live_incident_number(s)
        base = {"id": f"INC-{num}", "severity": "SEV2", "slack_channel": f"#{live_channel(s, app)}",
                "incident_lead": lead}
        incs.append(_inc({**base, "event": "incident.created", "ts": at,
                          "name": "Homepage TTR regression across all platforms"
                          if app == "homepage-graphql" else f"{app} degradation",
                          "status": "investigating", "affected_services": [guess]},
                         tags("incident")))
        incs.append(_inc({**base, "event": "incident.updated", "ts": at + CORRECTION_AFTER,
                          "status": "investigating", "affected_services": [app]},
                         tags("incident")))
    for d in w.degs_of("edge-gateway", "load_shed"):
        at = d.start_utc + timedelta(minutes=2)
        alerts.append(_alert(9100, at, "edge-gateway", "warning", "edge.requests.shed", "firing",
                             "Requests shed above 1% on edge-gateway"))
        pages.append(_page("P" + _hexid("shed", s.name, n=6).upper(), at + timedelta(seconds=40),
                           "edge-gateway", "low", "[WARN] edge-gateway shedding load",
                           "triggered", lead, key="radar-09100"))
    return alerts, pages, incs


def _inc(f: dict, tg=frozenset()) -> RawRecord:
    return RawRecord(source="events", rel_path="incidents/incident_io.jsonl",
                     fmt="incident_jsonl", at_utc=f["ts"], tags=tg, fields=f)


def _noise_alerts(w: World, rng) -> list[RawRecord]:
    s = w.scenario
    out = []
    quiet = [a.id for a in s.catalog if a.id not in w.path and not w.is_client(a.id)]
    for n in range(N_NOISE_ALERTS):
        app = quiet[int(rng.integers(len(quiet)))]
        summ, sev, metric = NOISE_ALERTS[int(rng.integers(len(NOISE_ALERTS)))]
        at = s.window.start_utc + timedelta(seconds=int(rng.integers(0, 5 * 3600)))
        out.append(_alert(100 + n, at, app, sev, metric, "firing", f"{summ} on {app}"))
        out.append(_alert(100 + n, at + timedelta(minutes=int(rng.integers(5, 26))), app, sev,
                          metric, "ok", f"{summ} on {app}"))
    if "artwork-svc" in w.apps:  # the chronic NPE keeps an error-rate alert flapping
        for k in range(6):
            at = s.window.start_utc + timedelta(hours=k, minutes=7)
            out.append(_alert(200 + k, at, "artwork-svc", "warning", "server.errors.rps",
                              "firing", "Error rate above baseline on artwork-svc"))
            out.append(_alert(200 + k, at + timedelta(minutes=18), "artwork-svc", "warning",
                              "server.errors.rps", "ok",
                              "Error rate above baseline on artwork-svc"))
    for k, c in enumerate(c for c in s.changes if c.kind == "deployment"):
        at = c.at_utc + timedelta(minutes=1)
        out.append(_alert(300 + k, at, c.app, "warning", "server.errors.rps", "firing",
                          f"5xx rate elevated on {c.app}"))
        out.append(_alert(300 + k, at + timedelta(minutes=3), c.app, "warning",
                          "server.errors.rps", "ok", f"5xx rate elevated on {c.app}"))
    return out


def _history(w: World, rng) -> tuple[list[RawRecord], list[RawRecord]]:
    s = w.scenario
    nums = history_numbers(s)
    pages, incs = [], []
    for h in s.history:
        num = nums[h.id]
        dur = timedelta(minutes=int(rng.integers(35, 190)))
        page_at = h.opened_utc - timedelta(minutes=int(rng.integers(2, 7)))
        pid = "P" + _hexid("hist", s.name, h.id, n=6).upper()
        title = f"[{h.severity}] {h.affected} degraded"
        for status, at in (("triggered", page_at), ("acknowledged", page_at + timedelta(minutes=4)),
                           ("resolved", h.opened_utc + dur)):
            pages.append(_page(pid, at, h.affected, "high", title, status, h.resolver_handle))
        base = {"id": f"INC-{num}", "severity": h.severity,
                "slack_channel": f"#{history_channel(s, h)}"}
        incs.append(_inc({**base, "event": "incident.created", "ts": h.opened_utc,
                          "name": f"{h.affected} degradation", "status": "investigating",
                          "affected_services": [h.affected]}))
        resolved = {**base, "event": "incident.resolved", "ts": h.opened_utc + dur,
                    "status": "resolved", "affected_services": [h.affected],
                    "summary": h.summary, "resolved_by": h.resolver_handle}
        if h.cause_app is not None:
            resolved["postmortem"] = {"contributing_service": h.cause_app,
                                      "contributing_change_kind": h.cause_change_kind}
        incs.append(_inc(resolved))
    return pages, incs


def render_events(scenario: Scenario, degradations: tuple[Degradation, ...],
                  rng: np.random.Generator) -> list[RawRecord]:
    w = World(scenario, degradations)
    changes: list[RawRecord] = []
    ab = [_rec("events/ab_allocations.csv", "ab_csv", {"header": True})]
    for c in sorted(scenario.changes, key=lambda c: (c.at_utc, c.id)):
        if c.kind in ("deployment", "client_release"):
            changes += _deploy(w, c, rng)
        elif c.kind == "config_change":
            changes.append(_config(w, c, rng))
        elif c.kind == "scaling":
            changes.append(_scaling(w, c))
        elif c.kind == "experiment_allocation":
            ab.append(_ab(w, c, rng))
    alerts, pages, incs = _live(w, rng)
    alerts += _noise_alerts(w, rng)
    hp, hi = _history(w, rng)
    by_time = sorted(changes + alerts + pages + hp + incs + hi,
                     key=lambda r: (r.rel_path, r.at_utc))
    return by_time + (ab if len(ab) > 1 else []) + _commits(w, rng)
