"""Derive every CQ's expected rows from what the deterministic generator
actually produces for the hero scenario (seed 7), and write `<cq>.expected.json`.

    uv run python ontology/cq/_derive_expected.py [--answers data/raw/_answers]

Ruling R20: expected rows are the RIGHT answers, so they come from the
generator itself, not from DESIGN assumptions:
  * events and chat: `e2ekg.generate.run.render_unmessed`, the generator's
    own pre-mess render (changes with their external ids (R22), alerts,
    pages, incidents; chat beats with handle, time and phase);
  * retractions: only `apply_mess` adds them, and its rng is shared by every
    injector over every record, so re-running it costs a full generation. CQ06
    therefore reads the `chat.retraction` injections from
    `<answers>/hero.manifest.json` (written by `e2ekg generate`), checks it is
    seed 7 and that every retracted message is a beat this generator renders,
    and fails clearly when it is missing or stale;
  * topology, propagation, answer key, as_of (`world.as_of_utc`).

The CQs model an investigator AS OF the R21 cutoff (alert + 30 min; hero
2026-10-14T23:00:00Z) where it matters: 03, 06, 09, 10, 11 and 12 stop at the
cutoff. 05 and 13 are post-incident questions (the TMS mitigation is posted
30 s after the cutoff; the affected-service correction lands at 23:14Z), so
they span the window. The other CQs have no live time dimension.

Rows project only stable values: catalog app URIs, change URIs built from
external ids, handles, labels, incident ids, kinds, counts and times; never
hash-derived node URIs. The time literals hard-coded in the .rq files are
checked against the generator here.
"""
import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

from e2ekg.generate.hero_chat import MITIGATION
from e2ekg.generate.mess._base import word_re
from e2ekg.generate.mess.chat import NICKNAMES
from e2ekg.generate.mess.identity import SLACK_ONLY
from e2ekg.generate.people import live_channel
from e2ekg.generate.render_events import external_ids
from e2ekg.generate.run import ANSWERS_DIR, render_unmessed
from e2ekg.generate.world import as_of_utc, mitigation_attempts
from e2ekg.graph.shacl import cq_question
from e2ekg.graph.uris import app_uri, change_uri
from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.propagation import propagate
from e2ekg.scenario.topology import downstream_closure, upstream_closure

CQ_DIR = Path(__file__).resolve().parent
REPO = CQ_DIR.parents[1]
DEFAULT_ANSWERS = REPO / "data" / "raw" / ANSWERS_DIR
LOG_CATEGORY = {"pool_exhaustion": "pool_exhaustion", "timeouts": "timeout"}
HELD_WITHIN = timedelta(minutes=10)     # CQ05: a revert this soon means the mitigation did not hold
_COMMIT_APP = re.compile(r"^\[([a-z0-9][a-z0-9-]*)\]")

S = hero_scenario()
DEG = propagate(S.fault, S.edges, S.window)
APPS = {a.id: a for a in S.catalog}
LINKS = S.answer_key.causal_chain
ROOT = S.answer_key.root_cause_change_id
AS_OF = as_of_utc(S)
EXTERNAL = external_ids(S)                       # external id -> scenario change id
EXTERNAL_OF = {v: k for k, v in EXTERNAL.items()}  # scenario change id -> external id


class GeneratorOutputMissing(RuntimeError):
    """A CQ needs generator output that only `e2ekg generate` writes."""


# --- generator truth --------------------------------------------------------

@cache
def unmessed():
    """The generator's own pre-mess render of the events and chat families
    (`render_unmessed`; both are independent of scale)."""
    return render_unmessed(S, families=("events", "chat"))


def events():
    return tuple(unmessed().records["events"])


def beats():
    return tuple(unmessed().beats)


@dataclass(frozen=True)
class RChange:
    external_id: str
    kind: str
    app: str | None
    at_utc: datetime


@cache
def rendered_changes() -> tuple[RChange, ...]:
    """One change per external id, as the Changes enricher must emit them
    (core.ttl ABox conventions), including renderer-side events with no
    scenario change behind them: deploy-shipped commits, the manual ASG
    bumps, the poller's property re-apply and the ASG resets (R23)."""
    out: dict[tuple[str, str], RChange] = {}
    for r in events():
        f = r.fields
        match r.fmt:
            case "spinnaker_jsonl":
                if f.get("status") != "SUCCEEDED":
                    continue
                kind = "client_release" if f.get("type") == "client_release" else "deployment"
                key, app = f["id"], f["app"]
            case "properties_log":
                kind, key, app = "config_change", f["id"], f["app"]
            case "asg_jsonl":
                kind, key, app = "scaling", f["activity_id"], f["app"]
            case "ab_csv":
                if f.get("header"):
                    continue
                kind, key, app = "experiment_allocation", f["allocation_id"], f["app"]
            case "gitlog":
                m = _COMMIT_APP.match(f.get("message", ""))
                kind, key, app = "commit", f["sha"], m.group(1) if m else None
            case _:
                continue
        out.setdefault((kind, key), RChange(key, kind, app, r.at_utc))
    # Every scenario change is rendered under its external id.
    assert {c.external_id for c in out.values()} >= set(EXTERNAL), "external id drift"
    return tuple(out.values())


@dataclass(frozen=True)
class RIncident:
    id: str
    severity: str
    opened: datetime
    affected: tuple[str, ...]       # current (latest record)
    previous: tuple[str, ...]       # superseded values, oldest first
    resolved_by: str | None
    cause_app: str | None
    cause_kind: str | None


@cache
def rendered_incidents() -> dict[str, RIncident]:
    recs: dict[str, list] = {}
    for r in events():
        if r.fmt == "incident_jsonl":
            recs.setdefault(r.fields["id"], []).append(r)
    out = {}
    for iid, rs in recs.items():
        rs.sort(key=lambda r: r.at_utc)
        created = next(r for r in rs if r.fields["event"] == "incident.created")
        current = tuple(rs[-1].fields["affected_services"])
        previous: list[str] = []
        for r in rs[:-1]:
            for a in r.fields["affected_services"]:
                if a not in current and a not in previous:
                    previous.append(a)
        resolved = next((r for r in rs if r.fields["event"] == "incident.resolved"), None)
        pm = (resolved.fields.get("postmortem") or {}) if resolved else {}
        out[iid] = RIncident(iid, created.fields["severity"], created.at_utc, current,
                             tuple(previous), resolved.fields.get("resolved_by") if resolved else None,
                             pm.get("contributing_service"), pm.get("contributing_change_kind"))
    return out


def live_incident() -> RIncident:
    live = [i for i in rendered_incidents().values() if in_window(i.opened)]
    assert len(live) == 1, live
    return live[0]


@cache
def alerts() -> dict[str, tuple[str, datetime]]:
    """Radar alert id -> (app, firing time)."""
    out: dict[str, tuple[str, datetime]] = {}
    for r in events():
        if r.fmt == "radar_jsonl" and r.fields["state"] == "firing":
            aid = r.fields["alert_id"]
            if aid not in out or r.at_utc < out[aid][1]:
                out[aid] = (r.fields["app"], r.at_utc)
    return out


@cache
def pages() -> dict[str, tuple[str, datetime]]:
    """PagerDuty page id -> (app, triggered time)."""
    out: dict[str, tuple[str, datetime]] = {}
    for r in events():
        if r.fmt == "pagerduty_jsonl" and r.fields["status"] == "triggered":
            pid = r.fields["id"]
            if pid not in out or r.at_utc < out[pid][1]:
                out[pid] = (r.fields["app"], r.at_utc)
    return out


def live_beats():
    channel = live_channel(S, live_incident().affected[0])
    return sorted((b for b in beats() if b.channel == channel), key=lambda b: b.at)


def mentions(text: str, app: str) -> bool:
    a = APPS[app]
    names = {app, *a.aliases, SLACK_ONLY.get(app), NICKNAMES.get(app)} - {None}
    return any(word_re(n).search(text) for n in names)


@dataclass(frozen=True)
class Retraction:
    app: str
    claim_at: datetime          # the message that is walked back
    at: datetime                # the retraction message


def retractions(answers: Path) -> list[Retraction]:
    """chat.retraction injections in the live channel, from the generator's manifest."""
    path = Path(answers) / f"{S.name}.manifest.json"
    if not path.exists():
        raise GeneratorOutputMissing(
            f"{path} is missing. CQ06's rows come from the chat.retraction injections of a real "
            f"generation; run `uv run e2ekg generate --scenario {S.name} --out data/raw` first "
            "(or pass --answers).")
    m = json.loads(path.read_text())
    if m.get("seed") != S.seed or m.get("name") != S.name:
        raise GeneratorOutputMissing(f"{path} is for {m.get('name')} seed {m.get('seed')}, "
                                     f"not {S.name} seed {S.seed}")
    channel = live_channel(S, live_incident().affected[0])
    times = {b.at for b in live_beats()}
    out = []
    for inj in m["injections"]:
        if inj["injector_id"] != "chat.retraction" or inj["rel_path"] != \
                f"slack/{channel}/messages.jsonl":
            continue
        d = json.loads(inj["detail"])
        r = Retraction(d["app"], datetime.fromisoformat(d["at_utc"]),
                       datetime.fromisoformat(d["retracted_at"]))
        if r.claim_at not in times:
            raise GeneratorOutputMissing(f"{path} is stale: retracted message at {r.claim_at} "
                                         "is not a beat the current generator renders; regenerate")
        out.append(r)
    return out


def manifest_scale(answers: Path) -> float:
    return json.loads((Path(answers) / f"{S.name}.manifest.json").read_text())["scale"]


# --- formatting ---------------------------------------------------------------

def a(app_id: str) -> str:
    return f"<{app_uri(app_id)}>"


def chg(c: RChange) -> str:
    return f"<{change_uri(S.name, c.kind, c.external_id)}>"


def ts(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def lit(dt: datetime) -> str:
    return f'"{ts(dt)}"^^xsd:dateTime'


def in_window(dt: datetime) -> bool:
    return S.window.start_utc <= dt < S.window.end_utc


def before_as_of(dt: datetime) -> bool:
    return S.window.start_utc <= dt < AS_OF


def last_quarter() -> tuple[datetime, datetime]:
    t = S.window.start_utc
    q0 = datetime(t.year, 3 * ((t.month - 1) // 3) + 1, 1, tzinfo=UTC)
    prev = datetime(q0.year - (q0.month == 1), (q0.month - 4) % 12 + 1, 1, tzinfo=UTC)
    return prev, q0


# The TTR regression: the chain's first client metric anomaly (15:20 PT).
REGRESSION = next(x for x in LINKS if x.signal == "metric_anomaly" and APPS[x.app].kind == "client")
START, END = lit(S.window.start_utc), lit(S.window.end_utc)


# Each returns (vars, rows, time literals the .rq must contain), or for a constraint
# check (vars, None, literals, constraints); `answers` is the generator's _answers directory.

def cq01(answers):
    """Rendered incidents (history and live) with their CURRENT affected app,
    owned by Team Bedrock and deployed in us-east-1. The live incident first
    named api-gateway (Bedrock, us-east-1) and was corrected; CQ01 does not
    count superseded values (inc:previouslyAffected)."""
    rows = [{"app": a(x), "severity": i.severity, "opened": ts(i.opened)}
            for i in rendered_incidents().values() for x in i.affected
            if APPS[x].owner_team == "Team Bedrock" and "us-east-1" in APPS[x].regions]
    return ["app", "severity", "opened"], rows, []


def cq02(answers):
    """Rendered changes on downstream_closure(homepage-graphql) in [regression-40m, regression)."""
    end = REGRESSION.at_utc
    start = end - timedelta(minutes=40)
    down = downstream_closure(S.edges, "homepage-graphql")
    rows = [{"change": chg(c), "kind": c.kind, "app": a(c.app)} for c in rendered_changes()
            if c.app in down and start <= c.at_utc < end]
    return ["change", "kind", "app"], rows, [lit(start), lit(end)]


def cq03(answers):
    """Non-client apps whose propagated degradation plants pool-exhaustion or
    deadline-exceeded log lines, starting before the as_of cutoff."""
    rows = [{"app": a(d.app), "category": LOG_CATEGORY[d.kind]} for d in DEG
            if d.kind in LOG_CATEGORY and APPS[d.app].kind != "client" and before_as_of(d.start_utc)]
    return ["app", "category"], rows, [START, lit(AS_OF)]


def cq04(answers):
    """Rendered resolved incidents whose postmortem has the root-cause change's
    (kind, app) shape and that opened last calendar quarter: the twin."""
    rc = next(c for c in S.changes if c.id == S.answer_key.root_cause_change_id)
    lo, hi = last_quarter()
    rows = [{"handle": i.resolved_by, "pastApp": a(x)} for i in rendered_incidents().values()
            for x in i.affected
            if (i.cause_kind, i.cause_app) == (rc.kind, rc.app) and lo <= i.opened < hi]
    return ["handle", "pastApp"], rows, [START, END, lit(lo), lit(hi)]


def cq05(answers):
    """The first authored mitigation beat in the live channel that names
    title-metadata-svc (as TMS): its poster and time, and whether it held, i.e.
    no config change re-applied on TMS within 10 min. Hero: "can we bump min to
    40 on the TMS ASG" at 16:00:30 PT, 30 s AFTER the as_of cutoff; the
    Archaius poller re-applies the property ~2 min later, so it did not hold.
    (The first mitigation overall is the 15:43 api-gateway rollback, before the
    cutoff; the fix that works, reverting the property, is only staged by
    17:55 and the channel never confirms it, so "the mitigation that worked"
    has no answer in the chat.)"""
    tms = S.fault.target
    first = next(b for b in live_beats()
                 if b.phase == MITIGATION and mentions(b.text, tms) and in_window(b.at))
    undo = [c for c in rendered_changes() if c.kind == "config_change" and c.app == tms
            and first.at < c.at_utc <= first.at + HELD_WITHIN]
    held = not undo
    # Cross-check with the renderer-side mitigation model (R23).
    assert held == (not any(first.at < m.reset_utc <= first.at + HELD_WITHIN
                            for m in mitigation_attempts(S)))
    rows = [{"handle": first.handle, "at": ts(first.at), "held": "true" if held else "false"}]
    return ["handle", "at", "held"], rows, [START, END]


def cq06(answers):
    """Apps named by chat.retraction messages in the live channel posted before
    the as_of cutoff. The injector walks back any message mentioning a
    red-herring app (rate 0.5), so the set is what generation actually drew:
    for hero seed 7 at scale 1.0, api-gateway's first retraction lands at
    23:00:57Z, after the cutoff."""
    apps = sorted({r.app for r in retractions(answers) if before_as_of(r.at)})
    return ["app"], [{"app": a(x)} for x in apps], [START, lit(AS_OF)]


def cq07(answers):
    """A constraint check, not rows (R38): the engine's path may be finer than the answer
    key's summary chain. The top chain roots at the root cause's change node, every app
    on it is one the fault's propagation degrades (with its catalog owner as team), and
    it includes the fault's app and the symptom's."""
    root = next(c for c in rendered_changes() if c.external_id == EXTERNAL_OF[ROOT])
    degraded = sorted({d.app for d in DEG} | {S.fault.target})
    return ["app", "team"], None, [START, END], {
        "root": f"<{change_uri(S.name, root.kind, root.external_id)}>",
        "apps_within": [{"app": a(x), "team": APPS[x].owner_team} for x in degraded],
        "apps_include": [a(S.fault.target), a(LINKS[-1].app)]}


def cq08(answers):
    """upstream_closure(title-metadata-svc)."""
    return ["app"], [{"app": a(x)} for x in sorted(upstream_closure(S.edges, S.fault.target))], []


def cq09(answers):
    """Rendered pages triggered between window start and the as_of cutoff."""
    apps = sorted({app for app, at in pages().values() if before_as_of(at)})
    return (["app", "team"], [{"app": a(x), "team": APPS[x].owner_team} for x in apps],
            [START, lit(AS_OF)])


def cq10(answers):
    """Rendered changes per kind between window start and the as_of cutoff."""
    counts: dict[str, int] = {}
    for c in rendered_changes():
        if before_as_of(c.at_utc):
            counts[c.kind] = counts.get(c.kind, 0) + 1
    return (["kind", "n"], [{"kind": k, "n": str(n)} for k, n in sorted(counts.items())],
            [START, lit(AS_OF)])


def cq11(answers):
    """Client apps with a propagated degradation starting before the as_of cutoff."""
    apps = sorted({d.app for d in DEG if APPS[d.app].kind == "client" and before_as_of(d.start_utc)})
    return ["app"], [{"app": a(x)} for x in apps], [START, lit(AS_OF)]


def cq12(answers):
    """Rendered Radar alerts firing between window start and the live incident's
    creation (noise, deploy blips and the chronic artwork-svc alert included)."""
    opened = live_incident().opened
    apps = sorted({app for app, at in alerts().values() if S.window.start_utc <= at < opened})
    return ["app"], [{"app": a(x)} for x in apps], [START, END]


def cq13(answers):
    """The live incident's rendered affected-service history: created with the
    wrong early guess, corrected 40 min later (23:14Z, after the cutoff)."""
    i = live_incident()
    rows = [{"incidentId": i.id, "previous": a(p), "current": a(c)}
            for p in i.previous for c in i.affected]
    return ["incidentId", "previous", "current"], rows, [START, END]


def cq14(answers):
    """Rendered history: SEV2 postmortems naming an api-gateway deployment, over all SEV2s."""
    hist = [i for i in rendered_incidents().values() if not in_window(i.opened)]
    sev2 = [i for i in hist if i.severity == "SEV2"]
    gw = [i for i in sev2 if (i.cause_app, i.cause_kind) == ("api-gateway", "deployment")]
    return (["support", "frequency"],
            [{"support": str(len(gw)), "frequency": format(len(gw) / len(sev2), ".12g")}], [])


def cq15(answers):
    """Rendered experiment allocations in the window on downstream_closure of
    the live incident's current affected app."""
    down = set().union(*(downstream_closure(S.edges, x) for x in live_incident().affected))
    rows = [{"change": chg(c), "app": a(c.app)} for c in rendered_changes()
            if c.kind == "experiment_allocation" and c.app in down and in_window(c.at_utc)]
    return ["change", "app"], rows, [START, END]


DERIVE = {"01": cq01, "02": cq02, "03": cq03, "04": cq04, "05": cq05, "06": cq06, "07": cq07,
          "08": cq08, "09": cq09, "10": cq10, "11": cq11, "12": cq12, "13": cq13, "14": cq14,
          "15": cq15}
NEEDS_MANIFEST = {"06"}


def derive(answers: Path = DEFAULT_ANSWERS, strict: bool = True) -> dict[str, dict]:
    """Expected payload per CQ stem. With strict=False, CQs whose generator
    output is missing are left out instead of raising."""
    out = {}
    for rq in sorted(CQ_DIR.glob("*.rq")):
        try:
            vars_, rows, literals, *constraints = DERIVE[rq.name[:2]](answers)
        except GeneratorOutputMissing:
            if strict:
                raise
            continue
        text = rq.read_text()
        for x in literals:
            assert x in text, f"{rq.name}: time literal {x} drifted from the generator"
        payload = {"question": cq_question(rq), "vars": vars_}
        if constraints:
            payload["constraints"] = constraints[0]
        else:
            payload["rows"] = sorted(rows, key=lambda r: json.dumps(r, sort_keys=True))
        if rq.name[:2] in NEEDS_MANIFEST:
            payload["derived_from"] = (f"{S.name} seed {S.seed} scale "
                                       f"{manifest_scale(answers):g} manifest")
        out[rq.stem] = payload
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--answers", type=Path, default=DEFAULT_ANSWERS)
    args = ap.parse_args(argv)
    try:
        payloads = derive(args.answers)
    except GeneratorOutputMissing as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for stem, payload in payloads.items():
        (CQ_DIR / f"{stem}.expected.json").write_text(json.dumps(payload, indent=2) + "\n")
        print(f"{stem}: {len(payload['rows'])} rows" if "rows" in payload else f"{stem}: constraints")
    return 0


if __name__ == "__main__":
    sys.exit(main())
