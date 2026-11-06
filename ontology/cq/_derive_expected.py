"""Derive every CQ's expected rows from the hero scenario (answer key,
topology, propagation, history) and write `<cq>.expected.json`.

    uv run python ontology/cq/_derive_expected.py

Rows project only stable values: catalog app URIs, scenario change URIs,
handles, labels, kinds, counts and scenario timestamps; never hash-derived
node URIs. Each function documents which scenario facts it reads. The time
literals hard-coded in the .rq files are checked against the scenario here.
"""
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from e2ekg.graph.shacl import cq_question
from e2ekg.graph.uris import app_uri, change_uri
from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.history import TWIN_RESOLVER
from e2ekg.scenario.propagation import propagate
from e2ekg.scenario.topology import downstream_closure, upstream_closure

CQ_DIR = Path(__file__).resolve().parent
S = hero_scenario()
DEG = propagate(S.fault, S.edges, S.window)
APPS = {a.id: a for a in S.catalog}
CHANGES = {c.id: c for c in S.changes}
LINKS = S.answer_key.causal_chain
# Propagated degradation kind -> error category of the log lines it plants (CQ03).
LOG_CATEGORY = {"pool_exhaustion": "pool_exhaustion", "timeouts": "timeout"}


def a(app_id: str) -> str:
    return f"<{app_uri(app_id)}>"


def ts(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def lit(dt: datetime) -> str:
    return f'"{ts(dt)}"^^xsd:dateTime'


def in_window(dt: datetime) -> bool:
    return S.window.start_utc <= dt < S.window.end_utc


def link(signal: str):
    return next(x for x in LINKS if x.signal == signal)


def live_incident():
    """The live incident opens at the chain's incident link, on its app."""
    x = link("incident")
    return x.app, x.at_utc


def chg(c) -> str:
    return f"<{change_uri(S.name, c.kind, c.id)}>"


def last_quarter() -> tuple[datetime, datetime]:
    t = S.window.start_utc
    q0 = datetime(t.year, 3 * ((t.month - 1) // 3) + 1, 1, tzinfo=UTC)
    prev = datetime(q0.year - (q0.month == 1), (q0.month - 4) % 12 + 1, 1, tzinfo=UTC)
    return prev, q0


# The TTR regression: the chain's first client metric anomaly (15:20 PT).
REGRESSION = next(x for x in LINKS if x.signal == "metric_anomaly" and APPS[x.app].kind == "client")


# Each returns (vars, rows, time literals the .rq must contain).

def cq01():
    """Catalog owner + regions; history affected/severity/opened; the live
    incident's current affected app."""
    incidents = [(h.affected, h.severity, h.opened_utc) for h in S.history]
    app, at = live_incident()
    incidents.append((app, "SEV2", at))
    rows = [{"app": a(x), "severity": sev, "opened": ts(t)} for x, sev, t in incidents
            if APPS[x].owner_team == "Team Bedrock" and "us-east-1" in APPS[x].regions]
    return ["app", "severity", "opened"], rows, []


def cq02():
    """Changes on downstream_closure(homepage-graphql) in [regression-40m, regression)."""
    end = REGRESSION.at_utc
    start = end - timedelta(minutes=40)
    down = downstream_closure(S.edges, "homepage-graphql")
    rows = [{"change": chg(c), "kind": c.kind, "app": a(c.app)} for c in S.changes
            if c.app in down and start <= c.at_utc < end]
    return ["change", "kind", "app"], rows, [lit(start), lit(end)]


def cq03():
    """Non-client apps whose propagated degradation plants pool-exhaustion or
    deadline-exceeded log lines inside the window."""
    rows = [{"app": a(d.app), "category": LOG_CATEGORY[d.kind]} for d in DEG
            if d.kind in LOG_CATEGORY and APPS[d.app].kind != "client" and in_window(d.start_utc)]
    return ["app", "category"], rows, [lit(S.window.start_utc), lit(S.window.end_utc)]


def cq04():
    """History incidents with the root-cause change's (kind, app) shape, opened
    last calendar quarter: the twin."""
    rc = CHANGES[S.answer_key.root_cause_change_id]
    lo, hi = last_quarter()
    rows = [{"handle": h.resolver_handle, "pastApp": a(h.affected)} for h in S.history
            if (h.cause_change_kind, h.cause_app) == (rc.kind, rc.app) and lo <= h.opened_utc < hi]
    assert [r["handle"] for r in rows] == [TWIN_RESOLVER]
    return ["handle", "pastApp"], rows, [lit(lo), lit(hi)]


def cq05():
    """DESIGN.md section 2: a mitigation ("bump min to 40") on the fault target
    is posted before anyone names the cause."""
    return (["kind", "app", "beforeRootCause"],
            [{"kind": "mitigation", "app": a(S.fault.target), "beforeRootCause": "true"}],
            [lit(S.window.start_utc)])


def cq06():
    """Apps of red-herring changes that are not on the causal chain: the wrong
    guesses the channel walks back (chat.retraction targets the same set)."""
    chain = {x.app for x in LINKS}
    apps = sorted({CHANGES[c].app for c in S.answer_key.red_herring_change_ids} - chain)
    return ["app"], [{"app": a(x)} for x in apps], [lit(S.window.start_utc)]


def cq07():
    """Distinct apps of the answer-key chain with their catalog owner."""
    apps = dict.fromkeys(x.app for x in LINKS)
    return ["app", "team"], [{"app": a(x), "team": APPS[x].owner_team} for x in apps], []


def cq08():
    """upstream_closure(title-metadata-svc)."""
    return ["app"], [{"app": a(x)} for x in sorted(upstream_closure(S.edges, S.fault.target))], []


def _alerting_apps() -> list[str]:
    """Radar fires on the chain's alert link app and on every load-shedding
    gateway (DESIGN.md: edge load shedding, a symptom that looks like a cause)."""
    apps = [x.app for x in LINKS if x.signal == "alert"]
    apps += [d.app for d in DEG if d.kind == "load_shed"]
    return list(dict.fromkeys(apps))


def cq09():
    """Each alert pages the on-call of the alerting app's owner."""
    return ["app", "team"], [{"app": a(x), "team": APPS[x].owner_team} for x in _alerting_apps()], []


def cq10():
    """Count of scenario changes per kind inside the window."""
    counts: dict[str, int] = {}
    for c in S.changes:
        if in_window(c.at_utc):
            counts[c.kind] = counts.get(c.kind, 0) + 1
    return ["kind", "n"], [{"kind": k, "n": str(n)} for k, n in sorted(counts.items())], []


def cq11():
    """Client apps with a propagated degradation inside the window."""
    apps = sorted({d.app for d in DEG if APPS[d.app].kind == "client" and in_window(d.start_utc)})
    return ["app"], [{"app": a(x)} for x in apps], []


def cq12():
    """Alerting apps whose degradation starts before the incident opens."""
    _, opened = live_incident()
    starts = {x.app: x.at_utc for x in LINKS if x.signal == "alert"}
    starts.update({d.app: d.start_utc for d in DEG if d.kind == "load_shed"})
    apps = [x for x in _alerting_apps() if starts[x] < opened]
    return ["app"], [{"app": a(x)} for x in apps], [lit(S.window.start_utc)]


def cq13():
    """The first affected service is the latest red-herring deployment's app
    (the wrong early guess); it is corrected to the incident link's app."""
    deploys = sorted((CHANGES[c] for c in S.answer_key.red_herring_change_ids
                      if CHANGES[c].kind == "deployment"), key=lambda c: c.at_utc)
    app, _ = live_incident()
    return ["previous", "current"], [{"previous": a(deploys[-1].app), "current": a(app)}], []


def cq14():
    """History: SEV2s caused by an api-gateway deployment over all SEV2s."""
    sev2 = [h for h in S.history if h.severity == "SEV2"]
    gw = [h for h in sev2 if h.cause_app == "api-gateway" and h.cause_change_kind == "deployment"]
    return (["support", "frequency"],
            [{"support": str(len(gw)), "frequency": format(len(gw) / len(sev2), ".12g")}], [])


def cq15():
    """Experiment allocations in the window on downstream_closure(incident app)."""
    app, _ = live_incident()
    down = downstream_closure(S.edges, app)
    rows = [{"change": chg(c), "app": a(c.app)} for c in S.changes
            if c.kind == "experiment_allocation" and c.app in down and in_window(c.at_utc)]
    return ["change", "app"], rows, [lit(S.window.start_utc), lit(S.window.end_utc)]


DERIVE = {"01": cq01, "02": cq02, "03": cq03, "04": cq04, "05": cq05, "06": cq06, "07": cq07,
          "08": cq08, "09": cq09, "10": cq10, "11": cq11, "12": cq12, "13": cq13, "14": cq14,
          "15": cq15}


def derive() -> dict[str, dict]:
    out = {}
    for rq in sorted(CQ_DIR.glob("*.rq")):
        vars_, rows, literals = DERIVE[rq.name[:2]]()
        text = rq.read_text()
        for x in literals:
            assert x in text, f"{rq.name}: time literal {x} drifted from the scenario"
        rows = sorted(rows, key=lambda r: json.dumps(r, sort_keys=True))
        out[rq.stem] = {"question": cq_question(rq), "vars": vars_, "rows": rows}
    return out


def main() -> int:
    for stem, payload in derive().items():
        (CQ_DIR / f"{stem}.expected.json").write_text(json.dumps(payload, indent=2) + "\n")
        print(f"{stem}: {len(payload['rows'])} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
