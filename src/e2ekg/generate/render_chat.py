"""slack/<channel>/messages.jsonl in Slack export message format (R13 JSONL).

The live incident channel: the hero's is hand-written (hero_chat.BEATS)
and topped up with chatter to exactly 200 messages from 50 users mentioning
15 services; a variant's is assembled from its causal chain and red
herrings. Every past incident in scenario.history gets its own channel of
10-40 messages at its historical time. Slack never carries change ids or
answer-key wording; `TMS` and other nicknames enter via the script and the
chat.* injectors."""
from datetime import datetime, timedelta

import numpy as np

from e2ekg.generate.hero_chat import (
    BEATS,
    FILLER_PLATFORMS,
    FILLER_QUIET,
    FILLER_SVC,
    FILLERS,
    HERO_MESSAGES,
    HERO_USERS,
)
from e2ekg.generate.people import (
    history_channel,
    history_numbers,
    live_channel,
    live_incident_number,
    oncall,
    profile,
    roster,
    user_id,
)
from e2ekg.generate.records import RawRecord
from e2ekg.generate.world import World
from e2ekg.scenario.history import TWIN_RESOLVER
from e2ekg.scenario.model import Scenario
from e2ekg.scenario.propagation import Degradation
from e2ekg.timeutil import PT

HISTORY_MESSAGES = (10, 40)
VARIANT_USERS = 30
_KIND_WORDS = {"deployment": "deploy", "config_change": "config change", "scaling": "scale-in",
               "experiment_allocation": "experiment allocation", "commit": "commit",
               "client_release": "client release"}


def _msg(channel: str, handle: str, at: datetime, text: str,
         thread_ts: datetime | None = None) -> RawRecord:
    f = {"type": "message", "user": user_id(handle), "text": text, "ts": at,
         "user_profile": profile(handle)}
    if thread_ts is not None:
        f["thread_ts"] = thread_ts
    return RawRecord(source="chat", rel_path=f"slack/{channel}/messages.jsonl", fmt="slack_jsonl",
                     fields=f, at_utc=at)


def _hhmm(dt: datetime) -> str:
    return dt.astimezone(PT).strftime("%H:%M")


def _twin(s: Scenario):
    return next((h for h in s.history if h.resolver_handle == TWIN_RESOLVER), None)


def _fill(rng, msgs: list[tuple], users: list[str], n_total: int, lo: datetime,
          hi: datetime) -> list[tuple]:
    """Top `msgs` [(at, handle, text, reply_to_at)] up to n_total with chatter,
    giving every not-yet-used user a line first."""
    used = {m[1] for m in msgs}
    taken = {m[0] for m in msgs}
    span = (hi - lo).total_seconds()
    out = list(msgs)
    unused = [u for u in users if u not in used]
    while len(out) < n_total:
        who = unused.pop(0) if unused else users[int(rng.integers(len(users)))]
        text = FILLERS[int(rng.integers(len(FILLERS)))].format(
            plat=FILLER_PLATFORMS[int(rng.integers(len(FILLER_PLATFORMS)))],
            svc=FILLER_SVC[int(rng.integers(len(FILLER_SVC)))],
            quiet=FILLER_QUIET[int(rng.integers(len(FILLER_QUIET)))])
        at = lo + timedelta(seconds=int(rng.integers(0, int(span))), microseconds=int(
            rng.integers(1, 999_999)))
        if at in taken:
            continue
        taken.add(at)
        out.append((at, who, text, None))
    return out


def _records(channel: str, msgs: list[tuple]) -> list[RawRecord]:
    return [_msg(channel, who, at, text, reply) for at, who, text, reply in sorted(
        msgs, key=lambda m: m[0])]


def _hero_channel(s: Scenario, rng) -> list[RawRecord]:
    lead = oncall(s)
    others = [u for u in roster(HERO_USERS + 12) if u not in (lead, TWIN_RESOLVER)]
    users = [lead, TWIN_RESOLVER, *others[:HERO_USERS - 2]]
    twin = _twin(s)
    fmt = {"twin": f"INC-{history_numbers(s)[twin.id]}" if twin else "an old incident",
           "twin_channel": f"#{history_channel(s, twin)}" if twin else "the archive"}
    day = s.window.start_utc.astimezone(PT)
    msgs: list[tuple] = []
    for h, m, sec, who, text, reply in BEATS:
        at = day.replace(hour=h, minute=m, second=sec, microsecond=int(
            rng.integers(1, 999_999))).astimezone(s.window.start_utc.tzinfo)
        handle = lead if who == "lead" else TWIN_RESOLVER if who == "twin" else others[who - 1]
        msgs.append((at, handle, text.format(**fmt), msgs[reply][0] if reply is not None else None))
    lo = msgs[0][0] + timedelta(seconds=30)
    msgs = _fill(rng, msgs, users, HERO_MESSAGES, lo, s.window.end_utc - timedelta(minutes=2))
    incident_app = next(iter(World(s, ()).links("incident")))
    return _records(live_channel(s, incident_app), msgs)


def _variant_channel(s: Scenario, rng) -> list[RawRecord]:
    w = World(s, ())
    inc = w.links("incident")
    if not inc:
        return []
    app, at0 = next(iter(inc.items()))
    lead = oncall(s)
    users = [lead, *[u for u in roster(VARIANT_USERS + 1) if u != lead][:VARIANT_USERS - 1]]
    by_id = {c.id: c for c in s.changes}

    def who():
        return users[1 + int(rng.integers(len(users) - 1))]
    t = [at0 + timedelta(seconds=20)]

    def at(step_s: int) -> datetime:
        t[0] += timedelta(seconds=step_s, microseconds=int(rng.integers(1, 999_999)))
        return t[0]
    msgs = [(t[0], lead, f"Opening INC-{live_incident_number(s)}: radar fired on {app}, client "
             "TTR is up on every platform", None)]
    for h in s.answer_key.red_herring_change_ids:
        c = by_id[h]
        q = at(90)
        msgs.append((q, who(), f"{c.app} had a {_KIND_WORDS[c.kind]} at {_hhmm(c.at_utc)}, "
                     "could it be that?", None))
        msgs.append((at(240), who(), f"looked at {c.app}: timing doesn't fit the first "
                     "symptoms, not it", q))
    for link in s.answer_key.causal_chain:
        what = {"metric_anomaly": "latency is way up", "log_error": "logs are full of errors",
                "slow_trace": "traces are slow", "failed_trace": "traces are failing"}.get(
            link.signal)
        if what and not w.is_client(link.app):
            msgs.append((at(150), who(), f"{link.app} {what} since {_hhmm(link.at_utc)}", None))
    root = by_id.get(s.answer_key.root_cause_change_id or "")
    if root is not None:
        msgs.append((at(300), who(), f"can we shift traffic away from {root.app} while we dig?",
                     None))
        msgs.append((at(600), who(), f"found it I think: the {_KIND_WORDS[root.kind]} on "
                     f"{root.app} at {_hhmm(root.at_utc)} lines up with the first symptom", None))
        msgs.append((at(120), lead, f"ok, rolling back the {_KIND_WORDS[root.kind]} on "
                     f"{root.app}", None))
    n = min(len(msgs) + 40, 120)
    msgs = _fill(rng, msgs, users, n, at0 + timedelta(seconds=30),
                 min(s.window.end_utc, t[0] + timedelta(minutes=30)))
    return _records(live_channel(s, app), msgs)


def _history_channel(s: Scenario, h, rng) -> list[RawRecord]:
    users = roster(30)
    n = int(rng.integers(HISTORY_MESSAGES[0], HISTORY_MESSAGES[1] + 1))
    num = history_numbers(s)[h.id]
    t = [h.opened_utc + timedelta(seconds=15)]

    def at(step_s: int) -> datetime:
        t[0] += timedelta(seconds=int(rng.integers(step_s // 2, step_s * 2)),
                          microseconds=int(rng.integers(1, 999_999)))
        return t[0]

    def who():
        return users[int(rng.integers(len(users)))]
    cause = (f"the {_KIND_WORDS[h.cause_change_kind]} on {h.cause_app}"
             if h.cause_change_kind else f"something in {h.cause_app}")
    beats = [(t[0], h.resolver_handle, f"Opening INC-{num} ({h.severity}): {h.affected} "
              "is degraded", None),
             (at(60), who(), f"seeing elevated errors on {h.affected}", None),
             (at(120), who(), f"anything deployed around {_hhmm(h.opened_utc)}?", None),
             (at(300), h.resolver_handle, f"looks like {cause}, reverting", None),
             (at(600), who(), f"{h.affected} recovering", None),
             (at(300), h.resolver_handle, f"resolved. summary: {h.summary}", None)]
    beats = beats[:n]
    if n > len(beats):
        filler = (("+1", None), ("ack", None), (f"watching {h.affected}", None),
                  ("thanks all", None), ("joining", None), ("any ETA?", None))
        extra = []
        span = max(60, int((t[0] - h.opened_utc).total_seconds()))
        taken = {b[0] for b in beats}
        while len(extra) < n - len(beats):
            x = h.opened_utc + timedelta(seconds=int(rng.integers(20, span)),
                                         microseconds=int(rng.integers(1, 999_999)))
            if x in taken:
                continue
            taken.add(x)
            text = filler[int(rng.integers(len(filler)))][0]
            extra.append((x, who(), text, None))
        beats += extra
    return _records(history_channel(s, h), beats)


def render_chat(scenario: Scenario, degradations: tuple[Degradation, ...],
                rng: np.random.Generator) -> list[RawRecord]:
    out = (_hero_channel(scenario, rng) if scenario.name == "hero"
           else _variant_channel(scenario, rng))
    for h in scenario.history:
        out += _history_channel(scenario, h, rng)
    return out
