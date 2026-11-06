"""chat.* injectors over Slack export messages (fields: type, user, text, ts,
optional thread_ts/subtype). Edits and retractions add new messages rather
than overwrite the original."""
import re
from bisect import bisect_left
from dataclasses import replace
from datetime import timedelta

from e2ekg.generate.mess._base import (
    by_path,
    injector,
    local_index,
    pick,
    set_fields,
    with_inserts,
    word_re,
)

TYPO_RATE, NICK_RATE, EDIT_RATE, RETRACT_RATE = 0.10, 0.5, 0.05, 0.5
NICKNAMES = {"row-ranker": "the ranker", "homepage-graphql": "HPG", "lolomo-svc": "lolomo",
             "edge-gateway": "zuul", "api-gateway": "the gateway",
             "title-batch-loader": "the batch loader", "evcache-titles": "evcache",
             "cassandra-titles": "C*"}
EDIT_NOTES = ("(edit: wrong graph, see the p90 panel)", "(edit: typo)",
              "(edit: times are PT)", "(edit: adding link to the dashboard)")


def _messages(self, recs):
    return [i for i, r in enumerate(recs) if self.eligible(r, {"slack_jsonl"})
            and isinstance(r.fields.get("text"), str) and not r.fields.get("subtype")]


@injector("chat.typo", ("chat",))
def typo(self, recs, rng, scenario):
    """Swap two adjacent letters inside one word."""
    def spots(text):
        return [(w0 + j) for w0, w in _words(text) if len(w) >= 4 and w.isalpha()
                for j in range(1, len(w) - 2) if w[j] != w[j + 1]]
    cands = [i for i in _messages(self, recs) if spots(recs[i].fields["text"])]
    out, inj, loc = list(recs), [], local_index(recs)
    for k in pick(rng, len(cands), TYPO_RATE):
        i = cands[k]
        before = recs[i].fields["text"]
        sp = spots(before)
        p = sp[int(rng.integers(len(sp)))]
        after = before[:p] + before[p + 1] + before[p] + before[p + 2:]
        out[i] = set_fields(recs[i], text=after)
        inj.append(self.record(recs[i].rel_path, idx=loc[i], at_utc=recs[i].at_utc,
                               before=before, after=after))
    return out, inj


def _words(text):
    pos = 0
    for w in text.split(" "):
        yield pos, w
        pos += len(w) + 1


@injector("chat.nickname", ("chat",))
def nickname(self, recs, rng, scenario):
    pats = {app: word_re(app) for app in NICKNAMES}
    cands = [(i, app) for i in _messages(self, recs) for app, p in pats.items()
             if p.search(recs[i].fields["text"])]
    out, inj, loc = list(recs), [], local_index(recs)
    for k in pick(rng, len(cands), NICK_RATE):
        i, app = cands[k]
        before = out[i].fields["text"]
        after = pats[app].sub(NICKNAMES[app], before, count=1)
        if after == before:
            continue
        out[i] = set_fields(out[i], text=after)
        inj.append(self.record(recs[i].rel_path, idx=loc[i], at_utc=recs[i].at_utc, app=app,
                               nickname=NICKNAMES[app], before=before, after=after))
    return out, inj


def _add_after(recs, adds):
    """Insert each new message after the last record of its file whose
    at_utc <= the new message's, scanning forward from the original."""
    files = {recs[i].rel_path for i, _ in adds}
    paths = by_path(recs, lambda r: r.rel_path in files)
    after: dict[int, list] = {}
    for i, new in adds:
        idxs = paths[recs[i].rel_path]
        j = i
        for pos in range(bisect_left(idxs, i) + 1, len(idxs)):
            k = idxs[pos]
            if recs[k].at_utc is not None and recs[k].at_utc > new.at_utc:
                break
            j = k
        after.setdefault(j, []).append(new)
    return with_inserts(recs, after)


@injector("chat.edit", ("chat",))
def edit(self, recs, rng, scenario):
    """A `message_changed` event follows the original, carrying both texts."""
    cands = _messages(self, recs)
    adds, inj, loc = [], [], local_index(recs)
    for k in pick(rng, len(cands), EDIT_RATE):
        i = cands[k]
        r = recs[i]
        f = r.fields
        at = r.at_utc + timedelta(seconds=int(rng.integers(45, 301)))
        new_text = f"{f['text']} {EDIT_NOTES[int(rng.integers(len(EDIT_NOTES)))]}"
        msg = {"type": "message", "user": f.get("user"), "text": new_text, "ts": f["ts"],
               "edited": {"user": f.get("user"), "ts": at}}
        prev = {"type": "message", "user": f.get("user"), "text": f["text"], "ts": f["ts"]}
        adds.append((i, replace(r, at_utc=at, fields={
            "type": "message", "subtype": "message_changed", "ts": at, "message": msg,
            "previous_message": prev})))
        inj.append(self.record(r.rel_path, idx=loc[i], at_utc=r.at_utc, edited_at=at,
                               before=f["text"], after=new_text))
    return _add_after(recs, adds), inj


@injector("chat.retraction", ("chat",))
def retraction(self, recs, rng, scenario):
    """A thread reply walks back a wrong guess about a red-herring app (named
    by id or, after chat.nickname, by nickname)."""
    by_id = {c.id: c for c in scenario.changes}
    chain = {link.app for link in scenario.answer_key.causal_chain}
    herrings = sorted({by_id[c].app for c in scenario.answer_key.red_herring_change_ids} - chain)
    pats = {a: re.compile("|".join(word_re(n).pattern for n in (a, NICKNAMES.get(a)) if n))
            for a in herrings}
    cands = [(i, a) for i in _messages(self, recs) for a, p in pats.items()
             if p.search(recs[i].fields["text"])]
    adds, inj, loc = [], [], local_index(recs)
    seen: set[int] = set()
    for k in pick(rng, len(cands), RETRACT_RATE):
        i, app = cands[k]
        if i in seen:
            continue
        seen.add(i)
        r = recs[i]
        at = r.at_utc + timedelta(seconds=int(rng.integers(120, 601)))
        adds.append((i, replace(r, at_utc=at, fields={
            "type": "message", "user": r.fields.get("user"),
            "text": f"scratch that, I don't think it's {app} after all", "ts": at,
            "thread_ts": r.fields["ts"]})))
        inj.append(self.record(r.rel_path, idx=loc[i], at_utc=r.at_utc, app=app, retracted_at=at))
    return _add_after(recs, adds), inj


INJECTORS = (typo, nickname, edit, retraction)
