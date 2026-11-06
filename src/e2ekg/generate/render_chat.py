"""slack/<channel>/messages.jsonl in Slack export message format (R13 JSONL).

The live incident channel: the hero's is hand-written (hero_chat.BEATS)
and topped up with chatter to exactly 200 messages from 50 users mentioning
15 services; a variant's is assembled from its changes and causal chain.
Every past incident in scenario.history gets its own channel of 10-40
messages at its historical time.

R21: every beat carries a phase. Before the as_of cutoff (alert + 30 min)
a live channel holds only observations, hypotheses (right and wrong),
questions, mitigations and chatter; verdicts come after it. Slack never
carries change ids or answer-key wording."""
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from e2ekg.generate.hero_chat import (
    BEATS,
    CHATTER,
    FILLER_PLATFORMS,
    FILLER_QUIET,
    FILLER_SVC,
    FILLERS,
    HERO_MESSAGES,
    HERO_USERS,
    HISTORY,
    HYPOTHESIS,
    MITIGATION,
    OBSERVATION,
    QUESTION,
    VERDICT,
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
from e2ekg.generate.world import World, as_of_utc
from e2ekg.scenario.history import TWIN_RESOLVER
from e2ekg.scenario.model import Scenario
from e2ekg.scenario.propagation import Degradation
from e2ekg.timeutil import PT

HISTORY_MESSAGES = (10, 40)
VARIANT_USERS = 30
VARIANT_FILLERS = 40
_KIND_WORDS = {"deployment": "deploy", "config_change": "config change", "scaling": "scale-in",
               "experiment_allocation": "experiment allocation", "commit": "commit",
               "client_release": "client release"}


@dataclass(frozen=True)
class Beat:
    channel: str
    at: datetime
    handle: str
    text: str
    phase: str
    reply_to: datetime | None = None


def _hhmm(dt: datetime) -> str:
    return dt.astimezone(PT).strftime("%H:%M")


def _us(rng) -> timedelta:
    return timedelta(microseconds=int(rng.integers(1, 999_999)))


def _fill(rng, channel: str, beats: list[Beat], users: list[str], n_total: int, lo: datetime,
          hi: datetime) -> list[Beat]:
    """Top `beats` up to n_total with chatter, giving every unused user a line first."""
    used = {b.handle for b in beats}
    taken = {b.at for b in beats}
    span = int((hi - lo).total_seconds())
    out = list(beats)
    unused = [u for u in users if u not in used]
    while len(out) < n_total:
        who = unused.pop(0) if unused else users[int(rng.integers(len(users)))]
        text = FILLERS[int(rng.integers(len(FILLERS)))].format(
            plat=FILLER_PLATFORMS[int(rng.integers(len(FILLER_PLATFORMS)))],
            svc=FILLER_SVC[int(rng.integers(len(FILLER_SVC)))],
            quiet=FILLER_QUIET[int(rng.integers(len(FILLER_QUIET)))])
        at = lo + timedelta(seconds=int(rng.integers(0, span))) + _us(rng)
        if at in taken:
            continue
        taken.add(at)
        out.append(Beat(channel, at, who, text, CHATTER))
    return out


def _hero_beats(s: Scenario, rng) -> list[Beat]:
    lead = oncall(s)
    others = [u for u in roster(HERO_USERS + 12) if u not in (lead, TWIN_RESOLVER)]
    users = [lead, TWIN_RESOLVER, *others[:HERO_USERS - 2]]
    twin = next((h for h in s.history if h.resolver_handle == TWIN_RESOLVER), None)
    fmt = {"twin": f"INC-{history_numbers(s)[twin.id]}" if twin else "an old incident",
           "twin_channel": f"#{history_channel(s, twin)}" if twin else "the archive"}
    channel = live_channel(s, next(iter(World(s, ()).links("incident"))))
    day = s.window.start_utc.astimezone(PT)
    beats: list[Beat] = []
    for h, m, sec, who, text, reply, phase in BEATS:
        at = (day.replace(hour=h, minute=m, second=sec, microsecond=0) + _us(rng)).astimezone(
            s.window.start_utc.tzinfo)
        handle = lead if who == "lead" else TWIN_RESOLVER if who == "twin" else others[who - 1]
        beats.append(Beat(channel, at, handle, text.format(**fmt), phase,
                          beats[reply].at if reply is not None else None))
    lo = beats[0].at + timedelta(seconds=30)
    return _fill(rng, channel, beats, users, HERO_MESSAGES, lo,
                 s.window.end_utc - timedelta(minutes=2))


def _spaced(rng, lo: datetime, hi: datetime, n: int) -> list[datetime]:
    """n increasing instants in [lo, hi), evenly spaced with jitter."""
    step = (hi - lo) / max(n, 1)
    return [lo + step * k + step * 0.8 * float(rng.random()) for k in range(n)]


def _variant_beats(s: Scenario, rng) -> list[Beat]:
    w = World(s, ())
    inc = w.links("incident")
    cut = as_of_utc(s)
    if not inc or cut is None:
        return []
    app, at0 = next(iter(inc.items()))
    channel = live_channel(s, app)
    lead = oncall(s)
    users = [lead, *[u for u in roster(VARIANT_USERS + 1) if u != lead][:VARIANT_USERS - 1]]

    def who() -> str:
        return users[1 + int(rng.integers(len(users) - 1))]
    by_id = {c.id: c for c in s.changes}
    suspects = sorted((by_id[i] for i in (*s.answer_key.red_herring_change_ids,
                                          s.answer_key.root_cause_change_id) if i),
                      key=lambda c: (c.at_utc, c.app))
    # Pre-cutoff: observations, hypotheses about every suspect (right and wrong), a
    # mitigation and a question. No verdicts.
    pre: list[tuple[str, str, str]] = [
        (lead, f"Opening INC-{live_incident_number(s)}: radar fired on {app}, client TTR is up "
               "on every platform", OBSERVATION)]
    for link in s.answer_key.causal_chain:
        what = {"metric_anomaly": "latency is way up", "log_error": "logs are full of errors",
                "slow_trace": "traces are slow"}.get(link.signal)
        if what and not w.is_client(link.app):
            pre.append((who(), f"{link.app} {what} since {_hhmm(link.at_utc)}", OBSERVATION))
    order = rng.permutation(len(pre) - 1) + 1
    pre = [pre[0], *(pre[int(i)] for i in order)]
    for c in suspects:
        pre.insert(1 + int(rng.integers(len(pre))), (who(), f"{c.app} had a "
                   f"{_KIND_WORDS[c.kind]} at {_hhmm(c.at_utc)}, could it be that?", HYPOTHESIS))
    pre.append((who(), f"can we shed some load at the edge to protect {app} while we dig?",
                MITIGATION))
    pre.append((who(), "anything else change in the last two hours?", QUESTION))
    beats: list[Beat] = []
    for (handle, text, phase), at in zip(pre, _spaced(rng, at0 + timedelta(seconds=20),
                                                      cut - timedelta(minutes=1), len(pre))):
        beats.append(Beat(channel, at, handle, text, phase))
    asked = {b.text.split(" had a ")[0]: b.at for b in beats if b.phase == HYPOTHESIS}
    root = by_id.get(s.answer_key.root_cause_change_id or "")
    post: list[tuple[str, str, str, datetime | None]] = []
    for c in suspects:
        if c is root:
            continue
        post.append((who(), f"looked at {c.app}: its {_KIND_WORDS[c.kind]} doesn't line up with "
                     "the first symptoms, not it", VERDICT, asked.get(c.app)))
    if root is not None:
        post.append((who(), f"found it I think: the {_KIND_WORDS[root.kind]} on {root.app} at "
                     f"{_hhmm(root.at_utc)} lines up with the first symptom", VERDICT,
                     asked.get(root.app)))
        post.append((lead, f"ok, rolling back the {_KIND_WORDS[root.kind]} on {root.app}",
                     MITIGATION, None))
    end = min(s.window.end_utc, cut + timedelta(minutes=45))
    for (handle, text, phase, reply), at in zip(post, _spaced(
            rng, cut + timedelta(minutes=2), end - timedelta(minutes=5), len(post))):
        beats.append(Beat(channel, at, handle, text, phase, reply))
    return _fill(rng, channel, beats, users, len(beats) + VARIANT_FILLERS,
                 at0 + timedelta(seconds=30), end)


def _history_beats(s: Scenario, h, rng) -> list[Beat]:
    users = roster(30)
    channel = history_channel(s, h)
    n = int(rng.integers(HISTORY_MESSAGES[0], HISTORY_MESSAGES[1] + 1))
    t = [h.opened_utc + timedelta(seconds=15)]

    def at(step_s: int) -> datetime:
        t[0] += timedelta(seconds=int(rng.integers(step_s // 2, step_s * 2))) + _us(rng)
        return t[0]

    def who() -> str:
        return users[int(rng.integers(len(users)))]
    cause = (f"the {_KIND_WORDS[h.cause_change_kind]} on {h.cause_app}"
             if h.cause_change_kind else f"something in {h.cause_app}")
    num = history_numbers(s)[h.id]
    lines = [(t[0], h.resolver_handle, f"Opening INC-{num} ({h.severity}): {h.affected} "
              "is degraded"),
             (at(60), who(), f"seeing elevated errors on {h.affected}"),
             (at(120), who(), f"anything deployed around {_hhmm(h.opened_utc)}?"),
             (at(300), h.resolver_handle, f"looks like {cause}, reverting"),
             (at(600), who(), f"{h.affected} recovering"),
             (at(300), h.resolver_handle, f"resolved. summary: {h.summary}")][:n]
    filler = ("+1", "ack", f"watching {h.affected}", "thanks all", "joining", "any ETA?")
    span = max(60, int((t[0] - h.opened_utc).total_seconds()))
    taken = {x[0] for x in lines}
    while len(lines) < n:
        x = h.opened_utc + timedelta(seconds=int(rng.integers(20, span))) + _us(rng)
        if x not in taken:
            taken.add(x)
            lines.append((x, who(), filler[int(rng.integers(len(filler)))]))
    return [Beat(channel, a, who_, text, HISTORY) for a, who_, text in lines]


def chat_beats(scenario: Scenario, rng: np.random.Generator) -> list[Beat]:
    """Every chat message the scenario produces, with its phase (before mess)."""
    out = (_hero_beats(scenario, rng) if scenario.name == "hero"
           else _variant_beats(scenario, rng))
    for h in scenario.history:
        out += _history_beats(scenario, h, rng)
    return out


def _record(b: Beat) -> RawRecord:
    f = {"type": "message", "user": user_id(b.handle), "text": b.text, "ts": b.at,
         "user_profile": profile(b.handle)}
    if b.reply_to is not None:
        f["thread_ts"] = b.reply_to
    return RawRecord(source="chat", rel_path=f"slack/{b.channel}/messages.jsonl",
                     fmt="slack_jsonl", fields=f, at_utc=b.at)


def beat_records(beats: list[Beat]) -> list[RawRecord]:
    """The Slack records for `beats`, ordered by (channel, time)."""
    return [_record(b) for b in sorted(beats, key=lambda b: (b.channel, b.at))]


def render_chat(scenario: Scenario, degradations: tuple[Degradation, ...],
                rng: np.random.Generator) -> list[RawRecord]:
    return beat_records(chat_beats(scenario, rng))
