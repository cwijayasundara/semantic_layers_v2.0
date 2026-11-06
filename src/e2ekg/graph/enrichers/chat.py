"""Chat enricher: Slack channels, messages and the claims extracted from them (Task 9).

  * chat:Thread per channel (ops:rawSelector: every Slack record of the channel),
    chat:forIncident the incident.io record whose slack_channel names it.
  * ops:Message per Slack record (edits included, prov:wasRevisionOf the
    original): chat:inThread, ops:atTime (Slack ts with its microseconds),
    chat:postedBy (handle from the user profile; a record without one borrows
    the handle another record of the same user id carries), chat:category,
    chat:sentiment, chat:isIncidentRelated, chat:classificationStatus
    ("classified", or "unclassified" with chat:Unclassified) and ops:rawSelector.
  * ops:Claim per extracted claim (also ops:Hypothesis / ops:Mitigation by
    kind): chat:claimKind, chat:about (the resolved app), chat:quote (the text
    span), ops:atTime and chat:postedBy of its message, prov:wasDerivedFrom the
    message.
  * chat:retracts (core.ttl): only the hypothesis the extractor linked (R33). "I don't
    think it's X" withdraws a suspicion; it does not make an observation about X false,
    so a walk-back with no compatible hypothesis retracts nothing. The retraction claim
    cites both Slack records the link rests on (its own message and the target's) with
    ops:rawSelector."""
from rdflib import Literal, URIRef
from rdflib.namespace import PROV, RDF

from e2ekg.graph.enricher import (
    NS, Enricher, json_fields, node, person, raw_selectors, read_table, string, ts,
)
from e2ekg.graph.uris import concept, scoped_uri

OPS, CHAT = NS["ops"], NS["chat"]
_CLASS = {"hypothesis": (OPS.Claim, OPS.Hypothesis), "mitigation": (OPS.Claim, OPS.Mitigation)}


def _handle(fields: dict) -> str | None:
    p = fields.get("user_profile")
    name = p.get("name") if isinstance(p, dict) else None
    return name if isinstance(name, str) and name else None


def message_triples(scenario: str, m: dict, handle: str | None, thread=None, revises=None):
    u = node("chat", "message", scenario, m["raw_id"])
    yield u, RDF.type, OPS.Message
    if thread is not None:
        yield u, CHAT.inThread, thread
    if m["ts_utc"] is not None:
        yield u, OPS.atTime, ts(m["ts_utc"])
    if handle:
        p, pt = person(handle, [m["raw_id"]])
        yield u, CHAT.postedBy, p
        yield from pt
    classified = m["status"] != "unclassified"
    yield u, CHAT.classificationStatus, string("classified" if classified else "unclassified")
    yield u, CHAT.category, (concept("message_category", m["category"]) if classified
                             else CHAT.Unclassified)
    if m["sentiment"]:
        yield u, CHAT.sentiment, concept("sentiment", m["sentiment"])
    if m["is_incident_related"] is not None:
        yield u, CHAT.isIncidentRelated, Literal(bool(m["is_incident_related"]))
    if revises is not None:
        yield u, PROV.wasRevisionOf, revises
    yield from raw_selectors(u, [m["raw_id"]])


class ChatEnricher(Enricher):
    name = "chat"
    reads = ("derived/chat_messages", "derived/chat_claims", "slack", "incidents")

    def fetch(self, ctx):
        msgs = read_table(ctx, "derived/chat_messages").sort("raw_id").to_dicts()
        claims = read_table(ctx, "derived/chat_claims").sort("claim_idx").to_dicts()
        slack = read_table(ctx, "slack", ["raw_id", "channel", "ts_raw", "user", "text",
                                         "thread_ts", "subtype", "fields"]).to_dicts()
        inc = read_table(ctx, "incidents", ["incident_id", "fields"]).to_dicts()
        return msgs, claims, slack, inc

    def analyze(self, raw):
        msgs, claims, slack, inc = raw
        by_raw = {r["raw_id"]: r for r in slack}
        handles: dict[str, str] = {}
        for r in sorted(slack, key=lambda r: r["raw_id"]):
            h = _handle(json_fields(r["fields"]))
            if h and r["user"]:
                handles.setdefault(r["user"], h)
        handle_of = {}
        for m in msgs:
            r = by_raw.get(m["raw_id"], {})
            handle_of[m["raw_id"]] = _handle(json_fields(r.get("fields"))) or handles.get(m["user"])
        incident_of = {}
        for r in inc:
            ch = str(json_fields(r["fields"]).get("slack_channel") or "").lstrip("#")
            if ch and r["incident_id"]:
                incident_of.setdefault(ch, r["incident_id"])
        msg_at = {m["raw_id"]: m["ts_utc"] for m in msgs}
        by_idx = {c["claim_idx"]: c for c in claims}
        retracts = {c["claim_idx"]: [by_idx[c["refers_to"]]] for c in claims
                    if c["kind"] == "retraction" and c["refers_to"] in by_idx
                    and by_idx[c["refers_to"]]["kind"] == "hypothesis"}
        return {"msgs": msgs, "claims": claims, "text": {k: v["text"] for k, v in by_raw.items()},
                "handle_of": handle_of, "incident_of": incident_of, "msg_at": msg_at,
                "retracts": retracts}

    def emit(self, r):
        sc = self.scenario
        in_channel: dict[str, list[str]] = {}
        for m in r["msgs"]:
            if m["channel"]:
                in_channel.setdefault(m["channel"], []).append(m["raw_id"])
        for ch in sorted(in_channel):
            t = scoped_uri("chat", "thread", sc, ch)
            yield t, RDF.type, CHAT.Thread
            yield from raw_selectors(t, in_channel[ch])
            if ch in r["incident_of"]:
                yield t, CHAT.forIncident, scoped_uri("inc", "incident", sc, r["incident_of"][ch])
        for m in r["msgs"]:
            thread = scoped_uri("chat", "thread", sc, m["channel"]) if m["channel"] else None
            revises = node("chat", "message", sc, m["edited_from_raw_id"]) \
                if m["edited_from_raw_id"] else None
            yield from message_triples(sc, m, r["handle_of"].get(m["raw_id"]), thread, revises)
        for c in r["claims"]:
            u = _claim(sc, c)
            for cls in _CLASS.get(c["kind"], (OPS.Claim,)):
                yield u, RDF.type, cls
            yield u, CHAT.claimKind, concept("claim_kind", c["kind"])
            if c["about_app_uri"]:
                yield u, CHAT.about, URIRef(c["about_app_uri"])
            text = r["text"].get(c["raw_id"]) or ""
            if c["span_end"] and c["span_end"] <= len(text):
                yield u, CHAT.quote, string(text[c["span_start"]:c["span_end"]])
            if (at := r["msg_at"].get(c["raw_id"])) is not None:
                yield u, OPS.atTime, ts(at)
            if h := r["handle_of"].get(c["raw_id"]):
                yield u, CHAT.postedBy, person(h)[0]
            yield u, PROV.wasDerivedFrom, node("chat", "message", sc, c["raw_id"])
            for t in r["retracts"].get(c["claim_idx"], ()):
                yield u, CHAT.retracts, _claim(sc, t)
                yield from raw_selectors(u, [c["raw_id"], t["raw_id"]])


def _claim(scenario: str, c: dict):
    return node("chat", "claim", scenario, c["raw_id"], c["claim_idx"])
