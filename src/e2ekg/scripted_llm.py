"""A deterministic keyword-rule LLMClient (R7: no FIREWORKS_API_KEY).

`ScriptedClient` answers the two schemas the pipeline sends, with plain regex rules
over the request JSON, so default tests and Task 12 can run the whole extraction
without a network. It is a stand-in, not a model: it is only as good as its rules,
which are written against the hero channel's phrasing. Live GLM quality is
unmeasured until a key exists.

  * `BatchExtraction` (chat): category, sentiment, claims with sentence spans, and
    retraction links to the latest earlier hypothesis about the same service in the
    batch (or the thread parent's). Like a model it reads past a transposed-letter typo
    (what chat.typo injects) in a word the rules know, surfaces short uppercase tokens
    ("TMS", "HPG") as service mentions unless they are generic ops acronyms ("ASG",
    "TTR"), and attributes a claim with no service of its own to the one service its
    thread's first message names (never to a thread that names several). It only
    attributes existing claims; it never creates one.
  * `Pick` (identity): choose the candidate whose initials spell the surface, or whose
    trailing words a nickname names ("the ranker", "the batch loader"), else none."""
import json
import re

_SERVICE_SUFFIX = ("svc", "api", "gateway", "loader", "ranker", "graphql", "allocator",
                   "titles", "web", "tv", "ios", "android", "db", "cache", "proxy", "router",
                   "worker", "store", "indexer", "resolver", "manager", "pipeline")
_KEBAB = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)+\b")
_ACRONYM = re.compile(r"\b[A-Z]{2,5}\b(?!-\d)")
_NOT_SERVICES = frozenset("""
    ASG TTR CPU NPE PT UTC QA PR OK ID DB FYI LGTM ASAP SEV SLO SLA API HTTP URL TV TBD ETA IMO
    AWS VPC ELB JVM GC TLS DNS TCP JSON CI CD UI UX TTL QPS RPS OOM EOD PM AM DM IIRC AFAIK WIP
    SSH CLI NPES
""".split())
_NICK = re.compile(rf"\bthe (?:[a-z]+ )?(?:{'|'.join(_SERVICE_SUFFIX)})\b")
_SENT = re.compile(r"(?:(?![.?!:]+(?:\s|$))[\s\S])+(?:[.?!:]+(?=\s|$)|$)")

_RETRACT = re.compile(r"scratch that|after all|ruling (?:them |it )?out", re.I)
_MITIGATE = re.compile(r"\b(roll(?:ing)? ?back|rollback|revert|bump(?:ed)?|submitted|"
                       r"paging|page the|apply(?:ing)?|put .* back|scale (?:tms )?out|"
                       r"warm-up plan|holding statement)\b", re.I)
_ROOT = re.compile(r"found it|that explains|it came from|root cause|capacity was cut|"
                   r"was changed from", re.I)
_HYPO = re.compile(r"->|\u2192|could this be|lines up|could the|is a scaling policy|\bmaybe\b|"
                   r"lots of candidates|marking .* as the affected|\bI think\b|looks like", re.I)
_OBS = re.compile(r"p9\d|p50|latency|errors?|logs?|traces?|cpu|regress|slow|scale-in|"
                  r"exhaust|deadline|pegged|up across|partial|didn't move|nullpointer|"
                  r"dashboard|desired|instances?|503", re.I)
_URGENT = re.compile(r"\b(page|paging|now|urgent|blow|asap)\b|!", re.I)
_NEG = re.compile(r"slow|down|fail|error|exhaust|regress|pegged|elevated|broken|burn|"
                  r"partial|deadline|saturated|scale-in", re.I)
_POS = re.compile(r"thanks|great|clean|healthy|finished|fine|approved|resolved", re.I)
_DECIDE = re.compile(r"^plan:|yes, do it|owner approved|going with|let's go|decided", re.I)
_INCIDENT = re.compile(r"incident|INC-|latency|errors?|regress|outage|slow|rollback|"
                       r"scale|exhaust|deadline|pool|\basg\b|p99|\bttr\b|degraded", re.I)


_WORD = re.compile(r"[A-Za-z]{4,}")


_INFLECTIONS = ("rolling reverted scaling scaled applied regressed regression exhausted exhaustion "
                "errors error deadlines instances instance candidates trace")
_LEXICON = frozenset(
    w.lower() for rx in (_RETRACT, _MITIGATE, _ROOT, _HYPO, _OBS, _URGENT, _NEG, _POS, _DECIDE,
                         _INCIDENT)
    for w in re.findall(r"[A-Za-z]{4,}", re.sub(r"\\.", " ", rx.pattern))) | set(_INFLECTIONS.split())


def _swapped(a: str, b: str) -> bool:
    """a is b with two adjacent letters transposed (what chat.typo makes). Dropped,
    added or replaced letters are left alone: they turn real inflections into keywords
    (approve -> approved)."""
    d = [i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y]
    return (len(a) == len(b) and len(d) == 2 and d[1] == d[0] + 1
            and a[d[0]] == b[d[1]] and a[d[1]] == b[d[0]])


def _repair(text: str) -> str:
    """Reads past typos the way a model does: a word one edit away from exactly one word
    the rules know becomes that word. Positions are not preserved; classify, don't slice."""
    def fix(m: re.Match) -> str:
        w = m.group().lower()
        if w in _LEXICON:
            return m.group()
        hits = [k for k in _LEXICON if _swapped(w, k)]
        return hits[0] if len(hits) == 1 else m.group()
    return _WORD.sub(fix, text)


def _services(text: str) -> list[str]:
    out = [m for m in _KEBAB.findall(text) if m.split("-")[-1] in _SERVICE_SUFFIX]
    out += [a for a in _ACRONYM.findall(text) if a not in _NOT_SERVICES]
    out += _NICK.findall(text)
    return list(dict.fromkeys(out))


def _bare_nicknames(items: list[dict]) -> frozenset[str]:
    """Lower-case first words of the service ids written in this batch ("evcache" for
    evcache-titles): a model reads them as the service in later messages."""
    ids = {s for it in items for s in _services(it["text"]) if "-" in s}
    return frozenset(s.split("-")[0] for s in ids if len(s.split("-")[0]) >= 5)


def _named(text: str, nicks: frozenset[str]) -> list[str]:
    return _services(text) + [w for w in re.findall(r"\b[a-z]{5,}\b", text) if w in nicks]


def _thread_subject(items: list[dict], it: dict, nicks: frozenset[str]) -> list[str]:
    """What a reply with no service of its own is about: the one service the thread's
    first message names. A thread that names none, or several, has no single subject, and
    a reply that names another service, even by nickname, is not about the thread's."""
    p = it.get("thread_parent")
    if p is None or not 0 <= p < len(items) or _named(it["text"], nicks):
        return []
    named = _named(items[p]["text"], nicks)
    return _services(items[p]["text"]) if len(named) == 1 else []


def _label(text: str) -> dict:
    text = _repair(text)
    if _DECIDE.search(text):
        cat = "decision"
    elif text.rstrip().endswith("?") or text.lower().startswith(("which ", "who ", "why ")):
        cat = "question"
    elif _INCIDENT.search(text):
        cat = "incident"
    else:
        cat = "discussion"
    if _URGENT.search(text):
        sent = "urgent"
    elif _NEG.search(text):
        sent = "negative"
    elif _POS.search(text):
        sent = "positive"
    else:
        sent = "neutral"
    return {"category": cat, "sentiment": sent,
            "is_incident_related": cat in ("incident", "decision") or bool(_INCIDENT.search(text))}


def _sentences(text: str) -> list[tuple[int, int]]:
    spans = [(m.start(), m.end()) for m in _SENT.finditer(text) if m.group().strip()]
    return spans or ([(0, len(text))] if text else [])


def _kind(sentence: str) -> str | None:
    sentence = _repair(sentence)
    for kind, rx in (("retraction", _RETRACT), ("root_cause", _ROOT),
                     ("mitigation", _MITIGATE), ("hypothesis", _HYPO)):
        if rx.search(sentence):
            return kind
    if _OBS.search(sentence) and not sentence.rstrip().endswith("?") and (
            _services(sentence) or re.search(r"p9\d|latency|ttr|logs|desired|instance", sentence,
                                             re.I)):
        return "observation"
    return None


class ScriptedClient:
    def __init__(self) -> None:
        self.batch_calls = 0
        self.calls = 0

    def structured(self, system: str, user: str, schema):
        from e2ekg.preprocess.chat_extract import BatchExtraction
        from e2ekg.preprocess.identity import Pick
        self.calls += 1
        if schema is BatchExtraction:
            self.batch_calls += 1
            return self._batch(json.loads(user)["messages"])
        if schema is Pick:
            return self._pick(json.loads(user))
        raise NotImplementedError(schema)

    @staticmethod
    def _pick(req: dict):
        """Initials (TMS), a squashed prefix (artworkmeta) or a token-wise abbreviation
        (homepage-gql, cass-titles); exactly one candidate must fit, else none."""
        from e2ekg.preprocess.identity import Pick
        word = req.get("core") or req["surface"]
        flat = re.sub(r"[^a-z]", "", word.lower())
        qt = [x for x in re.split(r"[^a-z0-9]+", word.lower()) if x]

        def subseq(a: str, b: str) -> bool:
            it = iter(b)
            return a[:1] == b[:1] and all(ch in it for ch in a)

        def fits(cand: str) -> bool:
            ct = [x for x in cand.split("-")]
            if qt[:1] == ["the"] and len(qt) > 1:
                return ct[-(len(qt) - 1):] == qt[1:]
            initials = "".join(p[0] for p in ct if p.isalpha())
            squashed = "".join(ct)
            if flat and flat == initials:
                return True
            if len(flat) >= 6 and squashed.startswith(flat):
                return True
            return len(qt) == len(ct) and all(
                a == b or (a.isalpha() and len(a) >= 3 and subseq(a, b)) for a, b in zip(qt, ct,
                                                                                      strict=True))
        ok = [c for c in req["candidates"] if c != "none" and fits(c)]
        return Pick(choice=ok[0] if len(ok) == 1 else "none")

    @staticmethod
    def _batch(items: list[dict]):
        from e2ekg.preprocess.chat_extract import BatchExtraction
        out, flat = [], []      # flat: (message n, claim dict) in output order
        nicks = _bare_nicknames(items)
        for n, it in enumerate(items):
            text = it["text"]
            claims = []
            thread_subject = _thread_subject(items, it, nicks)
            for s, e in _sentences(text):
                sentence = text[s:e]
                kind = _kind(sentence)
                if kind is None:
                    continue
                surfaces = _services(sentence) or (_services(text) if kind != "observation"
                                                   else [])
                if kind == "observation" and not surfaces:
                    surfaces = _services(text)
                surfaces = surfaces or thread_subject
                ref = None
                if kind == "retraction":
                    mine = {x.lower() for x in surfaces}
                    parent = it.get("thread_parent")
                    pool = [(k, c) for k, (m, c) in enumerate(flat) if c["kind"] == "hypothesis"
                            and m <= n]
                    same = [k for k, c in pool if mine & {x.lower() for x in c["about_surface"]}]
                    from_parent = [k for k, c in pool if parent is not None
                                   and flat[k][0] == parent]
                    ref = (from_parent or same or [None])[-1]
                c = {"kind": kind, "about_surface": surfaces, "text_span": (s, e),
                     "refers_to_claim_idx": ref}
                claims.append(c)
                flat.append((n, c))
            out.append({"label": _label(text), "claims": claims})
        # claims of this message count after the earlier messages' claims
        return BatchExtraction.model_validate({"messages": out})
