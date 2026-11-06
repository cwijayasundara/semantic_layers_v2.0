"""Closed-output extraction from Slack messages.

The model fills a pydantic schema whose enums are the SKOS schemes in
`ontology/schemes` (message_category, sentiment, claim_kind). Anything outside them
fails validation; after the client's retries that is an `LLMContractError`, and the
message is kept with `category="unclassified"`, `status="unclassified"` and a
quarantine row (`llm_contract`). No message is dropped.

Choices:
  * Claim kinds are the ontology's: observation, hypothesis, mitigation, root_cause,
    retraction. (The task text also listed evidence, resolution and question; the
    committed claim_kind scheme has no such concepts, and a question is a message
    category, not a claim. An observation is what the brief called evidence.)
  * Batching: the messages of one channel go to the model in chunks of 40 in time
    order, so every thread sits with its parent (a thread alone is mostly one
    message in these exports). If a chunk fails the contract, its messages are
    retried one by one before they are marked unclassified.
  * `refers_to_claim_idx` is an index into the claims of that chunk, flattened in
    message order. A retraction's final `refers_to` is the global `claim_idx` of a
    hypothesis claim, from the model when that reference is valid, else the
    hypothesis of the message it replies to, else an earlier hypothesis in its own thread,
    else null. Every link needs the same resolved app on both claims (a retraction with no
    app links only inside its thread), and `link_method` records which rule fired
    (model, thread_parent, thread). A channel-wide "latest same-app hypothesis" rule was
    dropped: it could mark the true-cause hypothesis as retracted.
  * A claim naming N surfaces becomes N rows (one app each) with the same kind and span.
  * `text_span` is character offsets into the message text. A span outside the text
    is replaced by the whole message.
  * Edit events (`message_changed`) are messages of their own, linked to the original
    through `edited_from_raw_id`; both versions are extracted.
  * Extraction covers every message, before and after the RCA cutoff; as_of
    filtering happens when the as-of store is harvested (R34). Slack `ts` stays a
    string (`ts`) and `ts_utc` keeps microseconds."""
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

import polars as pl
from pydantic import BaseModel, Field, ValidationError

from e2ekg.llm import LLMClient, LLMContractError, LLMTransportError
from e2ekg.preprocess.identity import Resolver

CATEGORIES = ("incident", "question", "discussion", "decision")
SENTIMENTS = ("positive", "neutral", "negative", "urgent")
CLAIM_KINDS = ("observation", "hypothesis", "mitigation", "root_cause", "retraction")
CHUNK = 40
PARALLEL_CALLS = 4

Category = Literal["incident", "question", "discussion", "decision"]
Sentiment = Literal["positive", "neutral", "negative", "urgent"]
ClaimKind = Literal["observation", "hypothesis", "mitigation", "root_cause", "retraction"]


class ThreadLabel(BaseModel):
    category: Category
    sentiment: Sentiment
    is_incident_related: bool


class Claim(BaseModel):
    kind: ClaimKind
    about_surface: list[str] = Field(default_factory=list)
    text_span: tuple[int, int]
    refers_to_claim_idx: int | None = None


class MessageExtraction(BaseModel):
    label: ThreadLabel
    claims: list[Claim] = Field(default_factory=list)


class BatchExtraction(BaseModel):
    messages: list[MessageExtraction]


SYSTEM = f"""You label Slack messages from incident channels and extract typed claims.
Answer with one entry per input message, in the same order.

For each message give:
- label.category, exactly one of: {", ".join(CATEGORIES)}
- label.sentiment, exactly one of: {", ".join(SENTIMENTS)}
- label.is_incident_related: true or false
- claims: zero or more typed statements, each with
  - kind, exactly one of: {", ".join(CLAIM_KINDS)}
    observation = a reported fact about the system (a graph, a log line, a metric)
    hypothesis = a proposed cause
    mitigation = a proposed or applied action to reduce impact
    root_cause = a statement naming the cause
    retraction = a walk-back of an earlier claim ("scratch that, I don't think it's X")
  - about_surface: the service names the claim is about, exactly as written in the message
  - text_span: [start, end) character offsets of the claim inside the message text
  - refers_to_claim_idx: for a retraction only, the index of the hypothesis claim it
    walks back, counting every claim you output for this request in order from 0; else null
Questions, greetings and reactions usually have no claims. Never invent values outside
the lists above."""


def _user_prompt(items: list[dict]) -> str:
    return json.dumps({"messages": items}, ensure_ascii=False)


def _call(llm: LLMClient, items: list[dict]) -> BatchExtraction:
    try:
        out = llm.structured(SYSTEM, _user_prompt(items), BatchExtraction)
    except ValidationError as e:
        raise LLMContractError(str(e)) from e
    except LLMContractError:
        raise
    except Exception as e:  # transport, rate limit: the messages stay, unclassified
        raise LLMTransportError(f"{type(e).__name__}: {e}") from e
    if len(out.messages) != len(items):
        raise LLMContractError(f"expected {len(items)} messages, got {len(out.messages)}")
    return out


def _edit_source(fields: str | None) -> str | None:
    """Original message ts of a message_changed event."""
    try:
        return json.loads(fields or "{}").get("message", {}).get("ts")
    except ValueError:
        return None


def extract_chat_full(slack_df: pl.DataFrame, resolver: Resolver, llm: LLMClient
                      ) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    df = slack_df.sort("channel", "ts_utc", "raw_id", nulls_last=True)
    rows = df.to_dicts()
    by_ts = {(r["channel"], r["ts_raw"]): r["raw_id"] for r in rows if not r["subtype"]}
    pos = {r["raw_id"]: i for i, r in enumerate(rows)}

    # -- model calls ---------------------------------------------------------
    results: dict[int, tuple[ThreadLabel, list[tuple[int, Claim]]] | None] = {}
    errors: dict[int, str] = {}
    chunks: list[list[int]] = []
    for ch in dict.fromkeys(r["channel"] for r in rows):
        idx = [i for i, r in enumerate(rows) if r["channel"] == ch]
        chunks += [idx[k:k + CHUNK] for k in range(0, len(idx), CHUNK)]
    def label(chunk: list[int]):
        local = {rows[i]["ts_raw"]: n for n, i in enumerate(chunk) if not rows[i]["subtype"]}
        items = [{"i": n, "user": rows[i]["user"], "ts": rows[i]["ts_raw"],
                  "thread_parent": local.get(rows[i]["thread_ts"]),
                  "text": rows[i]["text"] or ""} for n, i in enumerate(chunk)]
        try:
            return [(chunk, _call(llm, items))], {}
        except (LLMContractError, LLMTransportError):
            groups, failed = [], {}
            for n, i in enumerate(chunk):
                single = [{**items[n], "i": 0, "thread_parent": None}]
                try:
                    groups.append(([i], _call(llm, single)))
                except (LLMContractError, LLMTransportError) as e:
                    failed[i] = f"{type(e).__name__}: {e}"
            return groups, failed

    batch_of: dict[int, int] = {}      # message index -> id of the model call that answered it
    gid = 0
    # Chunks are independent model calls; a live call takes ~70 s, so they run concurrently.
    # map() keeps chunk order, so call ids and the output stay deterministic.
    with ThreadPoolExecutor(max_workers=PARALLEL_CALLS) as pool:
        labelled = list(pool.map(label, chunks))
    for groups, failed in labelled:
        for i, err in failed.items():
            results[i], errors[i] = None, err
        for members, out in groups:
            gid += 1
            flat = 0
            for i, m in zip(members, out.messages, strict=True):
                results[i] = (m.label, [(flat + k, c) for k, c in enumerate(m.claims)])
                batch_of[i] = gid
                flat += len(m.claims)

    # -- claims: validate, split by surface, number globally -----------------
    records: list[dict] = []
    flat_to_final: dict[tuple, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        res = results.get(i)
        if res is None:
            continue
        text = r["text"] or ""
        for flat, c in res[1]:
            if not text:
                continue
            s, e = c.text_span
            if not (0 <= s < e <= len(text)):
                s, e = 0, len(text)
            surfaces = [x.strip() for x in c.about_surface if x and x.strip()] or [None]
            for surf in dict.fromkeys(surfaces):
                uri = None
                if surf:
                    ctx = f"{text} [channel: {r['channel']}]"
                    uri = resolver.resolve(surf, context=ctx).app_uri
                rec = {"i": i, "kind": c.kind, "surface": surf, "uri": uri, "s": s, "e": e,
                       "ref": c.refers_to_claim_idx, "batch": batch_of[i],
                       "flat": flat, "refers_to": None, "idx": len(records)}
                flat_to_final[(batch_of[i], flat)].append(rec["idx"])
                records.append(rec)

    def root(i: int) -> tuple[str, str]:
        r = rows[i]
        return r["channel"], r["thread_ts"] or r["ts_raw"]

    def same_thread(a: int, b: int) -> bool:
        return root(a) == root(b)

    def compatible(rec: dict, tgt: dict, direct_parent: bool = False) -> bool:
        """A retraction may only walk back a claim about the same app. If it names an app
        the target must name the same one; an unresolved target cannot be verified, so it
        is refused unless it is the retraction's direct thread parent (the reply itself is
        the evidence). If the retraction names none, only a claim in its own thread qualifies."""
        if rec["uri"]:
            return tgt["uri"] == rec["uri"] or (direct_parent and tgt["uri"] is None)
        return same_thread(rec["i"], tgt["i"])

    hyp_by_msg: dict[int, list[dict]] = defaultdict(list)
    for rec in records:
        if rec["kind"] == "hypothesis":
            hyp_by_msg[rec["i"]].append(rec)
    for rec in records:
        if rec["kind"] != "retraction":
            continue
        row = rows[rec["i"]]
        target, method = None, None
        if rec["ref"] is not None:                            # 1. the model's reference
            cands = [records[k] for k in flat_to_final.get((rec["batch"], rec["ref"]), [])
                     if records[k]["kind"] == "hypothesis" and records[k]["i"] <= rec["i"]
                     and compatible(rec, records[k])]
            if cands:
                target, method = cands[0], "model"
        if target is None and row["thread_ts"]:               # 2. the message it replies to
            parent = by_ts.get((row["channel"], row["thread_ts"]))
            if parent is not None and parent in pos:
                cands = [h for h in hyp_by_msg.get(pos[parent], [])
                         if compatible(rec, h, direct_parent=True)]
                if cands:
                    target, method = cands[-1], "thread_parent"
        if target is None and rec["uri"]:                     # 3. earlier in its own thread
            earlier = [h for i in range(rec["i"]) if same_thread(i, rec["i"])
                       for h in hyp_by_msg.get(i, []) if compatible(rec, h)]
            if earlier:
                target, method = earlier[-1], "thread"
        rec["refers_to"] = target["idx"] if target else None
        rec["link_method"] = method

    # -- frames --------------------------------------------------------------
    quarantine = [{"raw_id": rows[i]["raw_id"], "source": "chat",
                   "rel_path": rows[i]["rel_path"] if "rel_path" in rows[i] else None,
                   "reason": ("llm_contract" if errors[i].startswith("LLMContractError")
                              else "llm_error"), "excerpt": rows[i]["text"], "format": "slack",
                   "detail": errors[i][:500]} for i in sorted(errors)]
    msgs = []
    for i, r in enumerate(rows):
        res = results.get(i)
        src = by_ts.get((r["channel"], _edit_source(r.get("fields")))) if r["subtype"] else None
        msgs.append({
            "raw_id": r["raw_id"], "channel": r["channel"], "ts": r["ts_raw"],
            "ts_utc": r["ts_utc"], "user": r["user"], "thread_ts": r["thread_ts"],
            "category": res[0].category if res else "unclassified",
            "sentiment": res[0].sentiment if res else None,
            "is_incident_related": res[0].is_incident_related if res else None,
            "status": "ok" if res else "unclassified", "edited_from_raw_id": src})
    msgs_df = pl.DataFrame(msgs, schema={
        "raw_id": pl.String, "channel": pl.String, "ts": pl.String,
        "ts_utc": df.schema["ts_utc"], "user": pl.String, "thread_ts": pl.String,
        "category": pl.String, "sentiment": pl.String, "is_incident_related": pl.Boolean,
        "status": pl.String, "edited_from_raw_id": pl.String})
    claims_df = pl.DataFrame(
        [{"raw_id": rows[c["i"]]["raw_id"], "claim_idx": c["idx"], "kind": c["kind"],
          "about_app_uri": c["uri"], "about_surface": c["surface"], "span_start": c["s"],
          "span_end": c["e"], "refers_to": c["refers_to"],
          "link_method": c.get("link_method")} for c in records],
        schema={"raw_id": pl.String, "claim_idx": pl.Int64, "kind": pl.String,
                "about_app_uri": pl.String, "about_surface": pl.String,
                "span_start": pl.Int64, "span_end": pl.Int64, "refers_to": pl.Int64,
                "link_method": pl.String})
    q_df = pl.DataFrame(quarantine, schema={
        "raw_id": pl.String, "source": pl.String, "rel_path": pl.String, "reason": pl.String,
        "excerpt": pl.String, "format": pl.String, "detail": pl.String})
    return msgs_df, claims_df, q_df


def extract_chat(slack_df: pl.DataFrame, resolver: Resolver, llm: LLMClient
                 ) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(messages_out, claims_out); see the module docstring."""
    msgs, claims, _ = extract_chat_full(slack_df, resolver, llm)
    return msgs, claims
