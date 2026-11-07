"""Changes enricher: deployments, client releases, config changes, scaling
events, experiment allocations and commits, by EXTERNAL id (R22).

Conventions (core.ttl header; CQ02/05/10/15 rely on them):
  * one change node per (kind, external id), at chg/<kind>/<scenario>/<external id>,
    with chg:changeId = the external id; every raw record of it is cited
    (ops:rawSelector);
  * a Spinnaker execution's ops:atTime is its SUCCEEDED event (an execution with
    no SUCCEEDED record keeps its node and raw ids but has no ops:atTime: it did
    not happen); `type: client_release` executions are ops:ClientRelease;
  * every ASG activity line (any status, the cancelled reset included) is an
    ops:ScalingEvent and every fast-property line (the poller's re-apply
    included) an ops:ConfigChange, each its own node;
  * a commit's ops:changedApp comes from its "[<app>]" subject prefix (resolved
    against the catalog) and is absent without one;
  * the A/B csv header row is not a change; every allocation cites it, since the
    allocation rows are read through its column names.
Who made the change (triggered_by / user / actor, when it is a person handle)
is prov:wasAssociatedWith an ops:Person."""
import re
from collections import defaultdict

from rdflib.namespace import RDF

from e2ekg.graph.enricher import (
    NS, Enricher, dec, finite, integer, json_fields, person, raw_selectors, read_table, recovered,
    string, ts,
)
from e2ekg.graph.uris import app_uri, change_uri, concept

OPS, CHG, EXP, PROV = NS["ops"], NS["chg"], NS["exp"], NS["prov"]
CLASS = {"deployment": OPS.Deployment, "client_release": OPS.ClientRelease,
         "config_change": OPS.ConfigChange, "scaling": OPS.ScalingEvent,
         "experiment_allocation": OPS.ExperimentAllocation, "commit": OPS.Commit}
_COMMIT_APP = re.compile(r"^\s*\[([A-Za-z0-9][A-Za-z0-9._-]*)\]")
_HANDLE = re.compile(r"^[a-z][a-z0-9_-]*\.[a-z][a-z0-9_-]*$")
_ID_KEYS = {"spinnaker": ("id",), "properties": ("id",), "asg": ("activity_id",),
            "ab_allocation": ("allocation_id",), "commit": ("sha",)}


def _kind(event_kind: str, event_type: str | None) -> str | None:
    return {"spinnaker": "client_release" if event_type == "client_release" else "deployment",
            "properties": "config_change", "asg": "scaling",
            "ab_allocation": "experiment_allocation", "commit": "commit"}.get(event_kind)


class ChangesEnricher(Enricher):
    name = "changes"
    reads = ("events", "catalog")

    def fetch(self, ctx):
        rows = read_table(ctx, "events").sort("raw_id").to_dicts()
        prefix_app: dict[str, str | None] = {}
        for r in rows:
            if r["event_kind"] == "commit":
                m = _COMMIT_APP.match(json_fields(r["fields"]).get("message") or "")
                if m and m.group(1) not in prefix_app:
                    prefix_app[m.group(1)] = ctx.resolver.resolve(m.group(1)).app
        return rows, prefix_app

    def analyze(self, raw):
        rows, prefix_app = raw
        changes: dict[tuple[str, str], dict] = {}
        headers = [r["raw_id"] for r in rows
                   if r["event_kind"] == "ab_allocation" and r["row_kind"] == "header"]
        for r in rows:
            if r["row_kind"] != "record":
                continue
            kind = _kind(r["event_kind"], r["event_type"])
            if kind is None:
                continue
            f = json_fields(r["fields"])
            ext = r["external_id"] or next((f[k] for k in _ID_KEYS[r["event_kind"]] if f.get(k)),
                                           None)
            if not ext:
                continue
            c = changes.setdefault((kind, str(ext)), {
                "kind": kind, "id": str(ext), "raw_ids": [], "at": None, "status": None,
                "app": None, "who": defaultdict(list), "fields": {}})
            c["raw_ids"].append(r["raw_id"])
            if kind == "experiment_allocation":
                c["raw_ids"] += headers
            for k in ("triggered_by", "user", "actor"):
                if isinstance(f.get(k), str) and _HANDLE.match(f[k]):
                    c["who"][f[k]].append(r["raw_id"])
            if kind == "commit":
                m = _COMMIT_APP.match(f.get("message") or "")
                c["app"] = prefix_app.get(m.group(1)) if m else None
            elif c["app"] is None and r["app_uri"]:
                c["app"] = r["app_uri"].rsplit("/", 1)[-1]
            earliest = r["ts_utc"] is not None and (c["at"] is None or r["ts_utc"] < c["at"])
            if kind in ("deployment", "client_release"):
                if r["status"] == "SUCCEEDED" and earliest:
                    c["at"], c["status"], c["fields"] = r["ts_utc"], r["status"], f
                elif not c["fields"]:
                    c["fields"] = f
            elif earliest:
                c["at"], c["status"], c["fields"] = r["ts_utc"], r["status"] or f.get("status"), f
        return [changes[k] for k in sorted(changes)]

    def emit(self, changes):
        for c in changes:
            s = change_uri(self.scenario, c["kind"], c["id"])
            f = c["fields"]
            yield s, RDF.type, CLASS[c["kind"]]
            yield s, CHG.changeId, string(c["id"])
            yield s, OPS.changeKind, concept("change_kind", c["kind"])
            if c["at"] is not None:
                yield s, OPS.atTime, ts(c["at"])
            if c["app"]:
                yield s, OPS.changedApp, app_uri(c["app"])
            if c["status"]:
                yield s, CHG.status, string(str(c["status"]))
            for h in sorted(c["who"]):
                p, pt = person(h, c["who"][h])
                yield s, PROV.wasAssociatedWith, p
                yield from pt
            yield from _details(s, c["kind"], f)
            yield from raw_selectors(s, c["raw_ids"])


def _details(s, kind: str, f: dict):
    def text(k):
        v = recovered(f, k)
        return None if v is None else str(v)
    if kind in ("deployment", "client_release") and text("version"):
        yield s, CHG.version, string(text("version"))
    if kind == "config_change":
        for k, p in (("property", CHG.property), ("old", CHG.oldValue), ("new", CHG.newValue)):
            if text(k) is not None:
                yield s, p, string(text(k))
    if kind == "scaling":
        for k, p in (("previous_capacity", CHG.previousCapacity),
                     ("desired_capacity", CHG.desiredCapacity)):
            v = recovered(f, k)
            if isinstance(v, int) or (isinstance(v, str) and v.isdigit()):
                yield s, p, integer(int(v))
    if kind == "experiment_allocation":
        for k, p in (("experiment", EXP.experiment), ("cell", EXP.cell)):
            if text(k):
                yield s, p, string(text(k))
        for k, p in (("old_allocation_pct", EXP.oldAllocationPct),
                     ("new_allocation_pct", EXP.newAllocationPct)):
            try:
                v = float(f.get(k))
            except (TypeError, ValueError):
                continue
            if finite(v):
                yield s, p, dec(v)
