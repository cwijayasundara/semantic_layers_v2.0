"""The "after the agent's data cut-off" ending: what happened once the agent stopped looking.

This is the one reader of the full store in `e2ekg.explain` (the case view reads the as-of
store), and everything it returns is labelled as after the cut-off. It reads the full store
and the processed tables, never the answer key.

  * `series`: the alert's metric, and the metric of each chain anomaly (the series key before
    `|` in its raw selector) on that anomaly's app, as one-minute means over every series of
    that metric on that app, from `onset - 30 min` to the window end. The alert's series is on
    the alert's app when that app reports the metric; otherwise (a TTR alert on the GraphQL
    tier, measured by the clients) it is the mean over every app that reports it, still
    labelled with the alert's app.
    Points come from the Atlas and Prometheus tables unified by `clean_metrics`
    (`derived/metrics_clean.parquet`: units in ms, counters as per-interval deltas).
  * `changes`: the changes on the alert app's downstream closure in `(as_of, window end]`,
    from the full store: the failed mitigations and the fix among them."""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

from e2ekg.graph.store import Store
from e2ekg.graph.uris import BASE, NS

LABEL = "after the agent's data cut-off"
BEFORE_ONSET = timedelta(minutes=30)
APP = BASE + "/app/application/"
_IRI = re.compile(r"[^\s<>\"{}|\\^`]+")


def _when(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _z(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _closure(store: Store, app: str) -> list[str]:
    """The app and every app it reaches over ops:dependsOn (the agent's `dependency_closure`
    down), uncapped."""
    rows = store.query(f"""SELECT DISTINCT ?n WHERE {{
        <{APP + app}> <{NS['ops']}dependsOn>+ ?n }}""") if _IRI.fullmatch(APP + app) else []
    return sorted({app, *(r["n"][len(APP):] for r in rows if r["n"].startswith(APP))})


def _changes(store: Store, apps: list[str], after: datetime, until: datetime, *,
             inclusive: bool = False) -> list[dict]:
    """Changes on `apps` in (after, until] ([after, until] with `inclusive`, as the tool's
    window), in time order: the agent's `changes_in_window` query and wording, without its row
    LIMIT or its character cap (a page, not a prompt)."""
    from e2ekg.agent.tools import PFX, _change_what
    values = " ".join(f"<{APP + a}>" for a in apps if _IRI.fullmatch(APP + a))
    if not values:
        return []
    rows = store.query(PFX + f"""SELECT ?c ?kind ?app ?t ?prop ?old ?new ?pcap ?dcap ?version
        ?status ?experiment ?cell ?oalloc ?nalloc WHERE {{
        VALUES ?app {{ {values} }}
        ?c chg:changeId ?id ; ops:changeKind ?kind ; ops:changedApp ?app ; ops:atTime ?t .
        OPTIONAL {{ ?c chg:property ?prop ; chg:oldValue ?old ; chg:newValue ?new }}
        OPTIONAL {{ ?c chg:desiredCapacity ?dcap ; chg:previousCapacity ?pcap }}
        OPTIONAL {{ ?c chg:version ?version }} OPTIONAL {{ ?c chg:status ?status }}
        OPTIONAL {{ ?c exp:experiment ?experiment ; exp:cell ?cell ;
                      exp:oldAllocationPct ?oalloc ; exp:newAllocationPct ?nalloc }}
        FILTER(?t {'>=' if inclusive else '>'} "{_z(after)}"^^xsd:dateTime && ?t <= "{_z(until)}"^^xsd:dateTime) }}
        ORDER BY ?t ?c""")
    return [{"uri": r["c"], "kind": r["kind"].rstrip("/").rsplit("/", 1)[-1],
             "app": r["app"][len(APP):], "at": r["t"], "what": _change_what(r)} for r in rows]


def _anomaly_metrics(store: Store, uris: list[str]) -> list[tuple[str, str]]:
    """(metric, app) of each chain URI that is an ops:Anomaly, in chain order."""
    uris = [u for u in uris if _IRI.fullmatch(u)]     # a report's URI is drawn, never queried raw
    if not uris:
        return []
    ops = NS["ops"]
    rows = store.query(f"""SELECT ?u ?sel ?app WHERE {{
        VALUES ?u {{ {' '.join(f'<{u}>' for u in uris)} }}
        ?u a <{ops}Anomaly> ; <{ops}rawSelector> ?sel ; <{ops}onApplication> ?app }}""")
    found = {r["u"]: (json.loads(r["sel"]).get("key") or "", r["app"]) for r in rows}
    return [(key.split("|", 1)[0], app[len(APP):]) for u in uris if u in found
            for key, app in [found[u]] if key and app.startswith(APP)]


def after_cutoff(full_store: Store, proc_root: Path, case_meta: dict,
                 chain_uris: list[str]) -> dict:
    from e2ekg.agent.tools import make_tools
    alert = case_meta["alert"]
    as_of = _when(case_meta["as_of"])
    onset = _when(case_meta.get("onset_at") or alert["at"])
    clean = pl.scan_parquet(Path(proc_root) / "derived" / "metrics_clean.parquet").filter(
        ~pl.col("gap") & pl.col("v").is_not_null())
    end = clean.select(pl.col("t").max()).collect().item()

    span = clean.filter(pl.col("t").is_between(onset - BEFORE_ONSET, end)) \
        .with_columns(pl.col("t").dt.truncate("1m"))

    def points(frame: pl.LazyFrame) -> list[list]:
        rows = frame.group_by("t").agg(pl.col("v").mean()).sort("t").collect()
        return [[_z(t), v] for t, v in rows.select("t", "v").iter_rows()]

    series: list[dict] = []
    if alert.get("metric"):
        of = span.filter(pl.col("name") == alert["metric"])
        own = of.filter(pl.col("app_uri") == APP + alert["app"])
        if own.select(pl.len()).collect().item() > 0:
            series.append({"name": alert["metric"], "app": alert["app"], "apps": [alert["app"]],
                           "aggregate": None, "points": points(own)})
        else:      # measured by other apps (TTR by the clients): their mean, said so
            apps = sorted(u[len(APP):] for u in of.select(pl.col("app_uri").unique())
                          .collect()["app_uri"] if u and u.startswith(APP))
            series.append({"name": alert["metric"], "app": alert["app"], "apps": apps,
                           "aggregate": "mean" if len(apps) > 1 else None, "points": points(of)})
    seen = {(s["name"], s["app"]) for s in series}
    for name, app in dict.fromkeys(_anomaly_metrics(full_store, chain_uris)):
        if (name, app) not in seen:
            series.append({"name": name, "app": app, "apps": [app], "aggregate": None,
                           "points": points(span.filter(
                               (pl.col("name") == name) & (pl.col("app_uri") == APP + app)))})

    changes = _changes(full_store, _closure(full_store, alert["app"]), as_of, end)
    return {"label": LABEL, "as_of": case_meta["as_of"], "series": series, "changes": changes}
