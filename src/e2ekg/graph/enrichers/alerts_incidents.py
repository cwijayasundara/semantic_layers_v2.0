"""Alerts, pages and incidents (core.ttl header conventions):

  * one ops:Alert per Radar alert id: ops:onApplication, ops:atTime = its
    earliest "firing" record (an alert seen only as "ok" has no time);
  * one ops:Page per PagerDuty page id: ops:onApplication, ops:atTime = its
    earliest "triggered" record, ops:assignedTo the assignee;
  * one ops:Incident per incident.io id: inc:incidentId, ops:atTime = the
    incident.created record, ops:severity, ops:affects = the latest record's
    affected services, inc:previouslyAffected = earlier ones it dropped (with
    inc:correctedAt, the first record carrying the current list), and from the
    resolution: ops:resolvedBy, ops:causeKind and ops:causeApp (postmortem);
  * one inc:IncidentVersion per incident.io record: inc:versionOf the incident,
    ops:atTime the record's time, and what that record itself states,
    ops:severity and inc:affectedService. The incident as known at time t is its
    latest version at or before t (R34), so a correction never erases what was
    known before it.
Every record of a node is cited (ops:rawSelector), duplicate webhooks included.
A severity the record lost to the evt.missing_field mess is read back from its
description; otherwise the incident's other records supply it. Service names
go through the identity resolver; a name that is not a catalog app is dropped."""
import re

from rdflib.namespace import RDF

from e2ekg.graph.enricher import (
    NS, Enricher, json_fields, node, person, raw_selectors, read_table, recovered, string, ts,
)
from e2ekg.graph.uris import CHANGE_KINDS, app_uri, concept, scoped_uri
from e2ekg.preprocess.identity import APP_URI_PREFIX

OPS, INC = NS["ops"], NS["inc"]
_SEVERITY = re.compile(r"\bsev[\s-]?([0-9])\b", re.I)


def severity(text) -> str | None:
    """SEV<n> from any surface form the severity scheme lists (SEV2, SEV 2, sev-2)."""
    m = _SEVERITY.search(str(text or ""))
    return f"SEV{m.group(1)}" if m else None


def first_by_id(rows: list[dict], id_col: str, status_col: str, status: str) -> dict[str, dict]:
    """id -> {raw_ids, at (earliest record with `status`), rows} over time-sorted rows."""
    out: dict[str, dict] = {}
    for r in rows:
        if not r[id_col]:
            continue
        d = out.setdefault(r[id_col], {"raw_ids": [], "at": None, "rows": []})
        d["raw_ids"].append(r["raw_id"])
        d["rows"].append(r)
        if r[status_col] == status and r["ts_utc"] is not None and d["at"] is None:
            d["at"] = r["ts_utc"]
    return out


class AlertsIncidentsEnricher(Enricher):
    name = "alerts_incidents"
    reads = ("alerts", "pages", "incidents", "catalog")

    def fetch(self, ctx):
        def rows(t):
            return read_table(ctx, t).sort("ts_utc", "raw_id", nulls_last=True).to_dicts()
        incidents = rows("incidents")
        names = set()
        for r in incidents:
            f = json_fields(r["fields"])
            names |= {x for x in f.get("affected_services") or () if isinstance(x, str)}
            pm = f.get("postmortem")
            if isinstance(pm, dict) and isinstance(pm.get("contributing_service"), str):
                names.add(pm["contributing_service"])
        apps = {n: ctx.resolver.resolve(n).app for n in sorted(names)}
        return rows("alerts"), rows("pages"), incidents, apps

    def analyze(self, raw):
        alerts, pages, incidents, apps = raw
        out_alerts = []
        for aid, d in sorted(first_by_id(alerts, "alert_id", "state", "firing").items()):
            out_alerts.append({"id": aid, "app": _app_of(d["rows"]), **d})
        out_pages = []
        for pid, d in sorted(first_by_id(pages, "page_id", "status", "triggered").items()):
            who = next((json_fields(r["fields"]).get("assignee") for r in d["rows"]
                        if json_fields(r["fields"]).get("assignee")), None)
            out_pages.append({"id": pid, "app": _app_of(d["rows"]), "who": who, **d})
        out_inc = []
        for iid, d in sorted(first_by_id(incidents, "incident_id", "event",
                                         "incident.created").items()):
            out_inc.append({"id": iid, **d, **_incident(d["rows"], apps)})
        return out_alerts, out_pages, out_inc

    def emit(self, result):
        alerts, pages, incidents = result
        sc = self.scenario
        for a in alerts:
            u = scoped_uri("alrt", "alert", sc, a["id"])
            yield u, RDF.type, OPS.Alert
            yield from _common(u, a)
        for p in pages:
            u = scoped_uri("alrt", "page", sc, p["id"])
            yield u, RDF.type, OPS.Page
            yield from _common(u, p)
            if p["who"]:
                who, pt = person(p["who"])
                yield u, OPS.assignedTo, who
                yield from pt
        for i in incidents:
            u = scoped_uri("inc", "incident", sc, i["id"])
            yield u, RDF.type, OPS.Incident
            yield u, INC.incidentId, string(i["id"])
            if i["at"] is not None:
                yield u, OPS.atTime, ts(i["at"])
            if i["severity"]:
                yield u, OPS.severity, concept("severity", i["severity"])
            for x in i["current"]:
                yield u, OPS.affects, app_uri(x)
            for x in i["previous"]:
                yield u, INC.previouslyAffected, app_uri(x)
            if i["corrected"] is not None:
                yield u, INC.correctedAt, ts(i["corrected"])
            if i["resolved_by"]:
                who, pt = person(i["resolved_by"])
                yield u, OPS.resolvedBy, who
                yield from pt
            if i["cause_kind"]:
                yield u, OPS.causeKind, concept("change_kind", i["cause_kind"])
            if i["cause_app"]:
                yield u, OPS.causeApp, app_uri(i["cause_app"])
            yield from raw_selectors(u, i["raw_ids"])
            for v in i["versions"]:
                vu = node("inc", "version", sc, v["raw_id"])
                yield vu, RDF.type, INC.IncidentVersion
                yield vu, INC.versionOf, u
                if v["at"] is not None:
                    yield vu, OPS.atTime, ts(v["at"])
                if v["severity"]:
                    yield vu, OPS.severity, concept("severity", v["severity"])
                for x in v["affected"]:
                    yield vu, INC.affectedService, app_uri(x)
                yield from raw_selectors(vu, [v["raw_id"]])


def _affected(f: dict, apps: dict[str, str | None]) -> list[str]:
    return list(dict.fromkeys(a for x in f.get("affected_services") or ()
                              if isinstance(x, str) and (a := apps.get(x))))


def _incident(rows: list[dict], apps: dict[str, str | None]) -> dict:
    fields = [json_fields(r["fields"]) for r in rows]
    sevs = [severity(r["severity"]) or severity(recovered(f, "severity"))
            for r, f in zip(rows, fields, strict=True)]
    sev = next((s for s in sevs if s), None)
    versions = [{"raw_id": r["raw_id"], "at": r["ts_utc"], "severity": s,
                 "affected": _affected(f, apps)}
                for r, f, s in zip(rows, fields, sevs, strict=True)]
    lists = [(r, v["affected"]) for r, f, v in zip(rows, fields, versions, strict=True)
             if f.get("affected_services")]
    current = lists[-1][1] if lists else []
    previous = list(dict.fromkeys(a for _, xs in lists for a in xs if a not in current))
    corrected = next((r["ts_utc"] for r, xs in lists if set(xs) == set(current)), None) \
        if previous else None
    resolved = next((f for r, f in zip(rows, fields, strict=True)
                     if r["event"] == "incident.resolved"), {})
    pm = resolved.get("postmortem") if isinstance(resolved.get("postmortem"), dict) else {}
    kind = pm.get("contributing_change_kind")
    cause = pm.get("contributing_service")
    return {"severity": sev, "versions": versions, "current": current, "previous": previous,
            "corrected": corrected, "resolved_by": resolved.get("resolved_by"),
            "cause_kind": kind if kind in CHANGE_KINDS else None,
            "cause_app": apps.get(cause) if cause else None}


def _app_of(rows: list[dict]) -> str | None:
    """Catalog id of the first record whose surface name resolved."""
    return next((r["app_uri"].removeprefix(APP_URI_PREFIX) for r in rows if r["app_uri"]), None)


def _common(u, d):
    if d["app"]:
        yield u, OPS.onApplication, app_uri(d["app"])
    if d["at"] is not None:
        yield u, OPS.atTime, ts(d["at"])
    yield from raw_selectors(u, d["raw_ids"])
