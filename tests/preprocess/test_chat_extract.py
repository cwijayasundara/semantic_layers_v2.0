import json
import os
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest
from pp_helpers import app_uri, derived, table
from pydantic import ValidationError

from e2ekg.llm import FakeClient, LLMContractError
from e2ekg.preprocess.chat_extract import (
    BatchExtraction,
    Claim,
    ThreadLabel,
    extract_chat,
    extract_chat_full,
)
from e2ekg.preprocess.identity import Resolver
from e2ekg.scenario.model import Application

ONTOLOGY = Path(__file__).resolve().parents[2] / "ontology" / "schemes"
CH = "inc-1-title-metadata-svc"

MESSAGES = [  # (ts, thread_ts, subtype, text)
    ("1000.000001", None, None, "Opening INC-1: homepage-graphql p99 is up, partial responses"),
    ("1010.000002", None, None,
     "api-gateway deployed at 15:18, that lines up. can we roll it back?"),
    ("1020.000003", "1010.000002", None,
     "scratch that, I don't think it's api-gateway after all"),
    ("1030.000004", None, None, "TMS p99 went from 40ms to 120ms around 15:05"),
    ("1040.000005", None, None, "can we bump min to 40 on the TMS ASG and let it scale back out?"),
    ("1050.000006", None, None, "which dashboard are we all looking at?"),
    ("1060.000007", None, "message_changed", "which dashboard are we all looking at? (edit: typo)"),
    ("1070.000008", None, None, ":eyes:"),
    ("1080.000009", None, None, "scratch that, I don't think it's row-ranker after all"),
    ("1090.000010", None, None,
     "found it: the TMS.asg.minInstances property was changed from 48 to 12 at 14:52"),
]


@pytest.fixture(scope="module")
def slack_fixture() -> pl.DataFrame:
    rows = []
    for i, (ts, thread, sub, text) in enumerate(MESSAGES):
        rid = f"chat|slack/{CH}/messages.jsonl|{i * 100}|99|{i:016x}"
        fields = {"type": "message", "ts": ts}
        if sub:
            fields |= {"subtype": sub, "message": {"text": text, "ts": "1050.000006"}}
        rows.append({
            "raw_id": rid, "source": "chat", "rel_path": f"slack/{CH}/messages.jsonl",
            "ts_raw": ts, "channel": CH, "user": f"U{i}", "text": text, "subtype": sub,
            "thread_ts": thread, "fields": json.dumps(fields),
            "ts_utc": datetime.fromtimestamp(int(ts.split(".")[0]), UTC).replace(
                microsecond=int(ts.split(".")[1])),
        })
    return pl.DataFrame(rows).with_columns(pl.col("ts_utc").dt.replace_time_zone("UTC"))


def _app(i):
    return Application(id=i, aliases=(), owner_team="Team X", tier=1, regions=(),
                       kind="service")


@pytest.fixture(scope="module")
def resolver(scripted_llm):
    ids = ["title-metadata-svc", "api-gateway", "row-ranker", "homepage-graphql",
           "client-tv", "artwork-svc"]
    return Resolver(tuple(_app(i) for i in ids), scripted_llm)


def msg_len(df, rid):
    return len(df.filter(pl.col("raw_id") == rid)["text"][0])


def test_out_of_scheme_category_rejected(slack_fixture, resolver):
    fake = FakeClient({FakeClient.ANY_KEY: LLMContractError("category 'outage' not allowed")})
    msgs, claims, quarantine = extract_chat_full(slack_fixture, resolver, fake)
    assert set(msgs["status"]) == {"unclassified"} and msgs.height == slack_fixture.height
    assert set(msgs["category"]) == {"unclassified"} and claims.height == 0
    assert quarantine.height == slack_fixture.height
    assert set(quarantine["reason"]) == {"llm_contract"}
    assert set(quarantine["raw_id"]) == set(slack_fixture["raw_id"])


def test_schema_rejects_out_of_taxonomy_values():
    with pytest.raises(ValidationError):
        ThreadLabel(category="outage", sentiment="neutral", is_incident_related=True)
    with pytest.raises(ValidationError):
        ThreadLabel(category="incident", sentiment="angry", is_incident_related=True)
    with pytest.raises(ValidationError):
        Claim(kind="evidence", about_surface=[], text_span=(0, 1), refers_to_claim_idx=None)
    with pytest.raises(ValidationError):
        BatchExtraction.model_validate({"messages": [{"label": {
            "category": "outage", "sentiment": "neutral", "is_incident_related": True},
            "claims": []}]})


def test_enums_match_the_ontology_schemes():
    from rdflib import Graph, Namespace
    from rdflib.namespace import SKOS

    from e2ekg.preprocess import chat_extract as ce

    def notations(name):
        g = Graph().parse(ONTOLOGY / f"{name}.ttl")
        return {str(o) for o in g.objects(None, SKOS.notation)}
    assert set(ce.CATEGORIES) | {"unclassified"} == notations("message_category")
    assert set(ce.SENTIMENTS) == notations("sentiment")
    assert set(ce.CLAIM_KINDS) == notations("claim_kind")
    assert Namespace


def test_retraction_links_to_hypothesis(slack_fixture, resolver, scripted_llm):
    _, claims = extract_chat(slack_fixture, resolver, scripted_llm)
    ret = claims.filter(pl.col("kind") == "retraction")
    assert ret.height >= 1
    r = ret.filter(pl.col("refers_to").is_not_null()).row(0, named=True)
    assert claims.filter(pl.col("claim_idx") == r["refers_to"])["kind"][0] == "hypothesis"
    # the thread reply retracts the api-gateway hypothesis it replies to
    src = claims.filter(pl.col("claim_idx") == r["refers_to"])
    assert slack_fixture.filter(pl.col("raw_id") == src["raw_id"][0])["ts_raw"][0] == "1010.000002"


def test_unlinked_retraction_has_null_target(slack_fixture, resolver, scripted_llm):
    _, claims = extract_chat(slack_fixture, resolver, scripted_llm)
    last = slack_fixture.filter(pl.col("ts_raw") == "1080.000009")["raw_id"][0]
    r = claims.filter((pl.col("raw_id") == last) & (pl.col("kind") == "retraction"))
    assert r.height == 1 and r["refers_to"][0] is None      # no row-ranker hypothesis exists


def test_text_span_is_inside_message(slack_fixture, resolver, scripted_llm):
    _, claims = extract_chat(slack_fixture, resolver, scripted_llm)
    assert claims.height > 0
    assert all(0 <= s < e <= msg_len(slack_fixture, rid) for rid, s, e in
               claims.select("raw_id", "span_start", "span_end").iter_rows())


def test_bad_llm_span_is_clamped_not_trusted(slack_fixture, resolver):
    from e2ekg.preprocess.chat_extract import BatchExtraction, MessageExtraction
    lab = ThreadLabel(category="incident", sentiment="neutral", is_incident_related=True)
    n = slack_fixture.height
    bad = BatchExtraction(messages=[
        MessageExtraction(label=lab, claims=[Claim(kind="observation", about_surface=[],
                                                   text_span=(5, 9999),
                                                   refers_to_claim_idx=None)])
        for _ in range(n)])
    _, claims = extract_chat(slack_fixture, resolver, FakeClient({FakeClient.ANY_KEY: bad}))
    assert all(0 <= s < e <= msg_len(slack_fixture, rid) for rid, s, e in
               claims.select("raw_id", "span_start", "span_end").iter_rows())


def test_all_messages_kept_and_labels_closed(slack_fixture, resolver, scripted_llm):
    msgs, _ = extract_chat(slack_fixture, resolver, scripted_llm)
    assert msgs.height == slack_fixture.height and set(msgs["status"]) == {"ok"}
    assert set(msgs["category"]) <= {"incident", "question", "discussion", "decision"}
    assert set(msgs["sentiment"]) <= {"positive", "neutral", "negative", "urgent"}


def test_edits_keep_both_versions(slack_fixture, resolver, scripted_llm):
    msgs, _ = extract_chat(slack_fixture, resolver, scripted_llm)
    orig = slack_fixture.filter(pl.col("ts_raw") == "1050.000006")["raw_id"][0]
    edit = slack_fixture.filter(pl.col("ts_raw") == "1060.000007")["raw_id"][0]
    e = msgs.filter(pl.col("raw_id") == edit)
    assert e["edited_from_raw_id"][0] == orig
    assert orig in set(msgs["raw_id"]) and msgs.filter(pl.col("raw_id") == orig)[
        "edited_from_raw_id"][0] is None


def test_slack_ts_microsecond_precision(slack_fixture, resolver, scripted_llm):
    msgs, _ = extract_chat(slack_fixture, resolver, scripted_llm)
    m = msgs.filter(pl.col("ts") == "1030.000004").row(0, named=True)
    assert m["ts_utc"].microsecond == 4 and m["ts_utc"].second == 1030 % 60


def test_slack_only_nickname_resolved_through_closed_list(slack_fixture, resolver, scripted_llm):
    _, claims = extract_chat(slack_fixture, resolver, scripted_llm)
    tms = claims.filter(pl.col("about_surface") == "TMS")
    assert tms.height >= 2
    assert set(tms["about_app_uri"]) == {app_uri("title-metadata-svc")}
    assert ("mitigation" in set(tms["kind"])) and ("root_cause" in set(tms["kind"])
                                                    or "observation" in set(tms["kind"]))


def test_batched_calls(slack_fixture, resolver):
    from fixtures.scripted_llm import ScriptedClient
    s = ScriptedClient()
    extract_chat(slack_fixture, resolver, s)
    assert 1 <= s.batch_calls <= 2        # one channel, one chunk: not one call per message


def test_hero_channel_with_scripted_client(preprocessed_hero, scripted_llm):
    from e2ekg.preprocess.identity import catalog_from_table
    slack = table(preprocessed_hero, "slack")
    res = Resolver(catalog_from_table(table(preprocessed_hero, "catalog")), scripted_llm)
    msgs, claims = extract_chat(slack, res, scripted_llm)
    assert msgs.height == slack.height and (msgs["status"] == "ok").all()
    assert claims.filter((pl.col("kind") == "mitigation")
                         & (pl.col("about_app_uri") == app_uri("title-metadata-svc"))).height >= 1
    ret = claims.filter(pl.col("kind") == "retraction")
    assert ret.height > 0
    hyp = set(claims.filter(pl.col("kind") == "hypothesis")["claim_idx"])
    linked = ret.filter(pl.col("refers_to").is_not_null())
    assert linked.height > 0 and set(linked["refers_to"]) <= hyp
    # a refuting observation is not a retraction
    refute = slack.filter(pl.col("text").str.contains("rollback finished and TTR didn't move"))
    assert refute.height >= 1
    assert claims.filter(pl.col("raw_id").is_in(refute["raw_id"].to_list())
                         & (pl.col("kind") == "retraction")).height == 0
    d = derived(preprocessed_hero, "chat_messages")
    assert d.height == slack.height
    assert derived(preprocessed_hero, "chat_claims").height == claims.height


@pytest.mark.live
def test_live_glm_labels_hero_channel(preprocessed_hero):
    if not os.environ.get("FIREWORKS_API_KEY"):
        pytest.skip("FIREWORKS_API_KEY not set (deferred, pending key; R7)")
    from e2ekg.llm import FireworksClient
    from e2ekg.preprocess.identity import catalog_from_table
    client = FireworksClient()
    slack = table(preprocessed_hero, "slack")
    res = Resolver(catalog_from_table(table(preprocessed_hero, "catalog")), client)
    msgs, claims = extract_chat(slack, res, client)
    assert (msgs["status"] == "ok").mean() >= 0.95
    assert claims.filter((pl.col("kind") == "mitigation")
                         & (pl.col("about_app_uri") == app_uri("title-metadata-svc"))).height >= 1


def _frame(msgs):
    """msgs: (ts, thread_ts, text) in one channel."""
    rows = []
    for i, (ts, thread, text) in enumerate(msgs):
        rows.append({"raw_id": f"chat|slack/{CH}/m.jsonl|{i * 100}|9|{i:016x}",
                     "source": "chat", "rel_path": f"slack/{CH}/m.jsonl", "ts_raw": ts,
                     "channel": CH, "user": f"U{i}", "text": text, "subtype": None,
                     "thread_ts": thread, "fields": "{}",
                     "ts_utc": datetime.fromtimestamp(int(ts.split(".")[0]), UTC).replace(
                         microsecond=int(ts.split(".")[1]))})
    return pl.DataFrame(rows).with_columns(pl.col("ts_utc").dt.replace_time_zone("UTC"))


def test_retraction_never_links_to_an_unrelated_hypothesis(resolver, scripted_llm):
    """A walk-back about TMS must not retract the api-gateway hypothesis (or, worse, mark
    the true-cause TMS hypothesis as retracted when it is about something else)."""
    df = _frame([
        ("2000.000001", None, "api-gateway deployed at 15:18, that lines up. can we roll it back?"),
        ("2001.000001", None, "maybe the TMS fast property is the cause, could the floor be low?"),
        ("2002.000001", None, "scratch that, I don't think it's row-ranker after all"),
        ("2003.000001", None, "scratch that, I don't think it's title-metadata-svc after all"),
    ])
    _, claims = extract_chat(df, resolver, scripted_llm)
    r = claims.filter(pl.col("kind") == "retraction").sort("claim_idx")
    by_text = dict(zip(r["about_surface"], r["refers_to"], strict=True))
    assert by_text["row-ranker"] is None                       # no row-ranker hypothesis
    # title-metadata-svc: the TMS hypothesis is top-level and not in the retraction's thread
    assert by_text["title-metadata-svc"] is None
    assert set(r["link_method"]) <= {None}


def test_model_reference_to_another_app_is_refused(resolver):
    from e2ekg.preprocess.chat_extract import MessageExtraction
    df = _frame([
        ("3000.000001", None, "api-gateway deployed at 15:18, that lines up."),
        ("3001.000001", None, "scratch that, I don't think it's title-metadata-svc after all"),
    ])
    lab = ThreadLabel(category="incident", sentiment="neutral", is_incident_related=True)
    out = BatchExtraction(messages=[
        MessageExtraction(label=lab, claims=[Claim(kind="hypothesis",
                                                   about_surface=["api-gateway"],
                                                   text_span=(0, 20), refers_to_claim_idx=None)]),
        MessageExtraction(label=lab, claims=[Claim(kind="retraction",
                                                   about_surface=["title-metadata-svc"],
                                                   text_span=(0, 20), refers_to_claim_idx=0)])])
    _, claims = extract_chat(df, resolver, FakeClient({FakeClient.ANY_KEY: out}))
    r = claims.filter(pl.col("kind") == "retraction")
    assert r["refers_to"][0] is None


def test_same_app_links_inside_a_thread_and_records_method(resolver, scripted_llm):
    df = _frame([
        ("4000.000001", None, "api-gateway deployed at 15:18, that lines up. can we roll it back?"),
        ("4001.000001", "4000.000001", "scratch that, I don't think it's api-gateway after all"),
    ])
    _, claims = extract_chat(df, resolver, scripted_llm)
    r = claims.filter(pl.col("kind") == "retraction").row(0, named=True)
    assert r["refers_to"] is not None and r["link_method"] in ("model", "thread_parent")


def _two_message_extraction(hyp_surface, ret_surface, ret_ref=None):
    from e2ekg.preprocess.chat_extract import MessageExtraction
    lab = ThreadLabel(category="incident", sentiment="neutral", is_incident_related=True)
    return BatchExtraction(messages=[
        MessageExtraction(label=lab, claims=[Claim(kind="hypothesis", about_surface=hyp_surface,
                                                   text_span=(0, 10), refers_to_claim_idx=None)]),
        MessageExtraction(label=lab, claims=[Claim(kind="retraction", about_surface=ret_surface,
                                                   text_span=(0, 10),
                                                   refers_to_claim_idx=ret_ref)])])


def test_retraction_links_to_its_direct_thread_parent_even_if_that_hypothesis_is_unresolved(
        resolver):
    """'looks like the deploy on the gateway, reverting' can name an app the resolver cannot
    place; a reply 'scratch that, it's not api-gateway' still walks back its own parent."""
    df = _frame([
        ("5000.000001", None, "looks like the deploy on the gateway, reverting it now"),
        ("5001.000001", "5000.000001", "scratch that, I don't think it's api-gateway after all"),
    ])
    out = _two_message_extraction(["zzz-unknown-thing"], ["api-gateway"])
    _, claims = extract_chat(df, resolver, FakeClient({FakeClient.ANY_KEY: out}))
    hyp = claims.filter(pl.col("kind") == "hypothesis").row(0, named=True)
    ret = claims.filter(pl.col("kind") == "retraction").row(0, named=True)
    assert hyp["about_app_uri"] is None
    assert ret["refers_to"] == hyp["claim_idx"] and ret["link_method"] == "thread_parent"


def test_retraction_naming_an_app_still_refuses_an_unresolved_hypothesis_outside_its_parent(
        resolver):
    df = _frame([
        ("5100.000001", None, "looks like the deploy on the gateway, reverting it now"),
        ("5101.000001", None, "scratch that, I don't think it's api-gateway after all"),
    ])
    out = _two_message_extraction(["zzz-unknown-thing"], ["api-gateway"], ret_ref=0)
    _, claims = extract_chat(df, resolver, FakeClient({FakeClient.ANY_KEY: out}))
    assert claims.filter(pl.col("kind") == "retraction")["refers_to"][0] is None


def test_direct_parent_rule_does_not_cross_to_another_resolved_app(resolver):
    df = _frame([
        ("5200.000001", None, "api-gateway deployed at 15:18, that lines up."),
        ("5201.000001", "5200.000001", "scratch that, I don't think it's title-metadata-svc"),
    ])
    out = _two_message_extraction(["api-gateway"], ["title-metadata-svc"])
    _, claims = extract_chat(df, resolver, FakeClient({FakeClient.ANY_KEY: out}))
    assert claims.filter(pl.col("kind") == "retraction")["refers_to"][0] is None


def test_transport_errors_leave_unclassified_rows(slack_fixture, resolver):
    class Flaky:
        def structured(self, system, user, schema):
            raise ConnectionError("429 rate limited")
    msgs, claims, q = extract_chat_full(slack_fixture, resolver, Flaky())
    assert msgs.height == slack_fixture.height and set(msgs["status"]) == {"unclassified"}
    assert set(q["reason"]) == {"llm_error"} and claims.height == 0


def test_fireworks_client_transport_error_and_atomic_cache(tmp_path, monkeypatch):
    import sys
    import types

    from pydantic import BaseModel

    from e2ekg.llm import FireworksClient, LLMTransportError

    class Pick1(BaseModel):
        choice: str

    attempts = []

    class Chat:
        def __init__(self, model):
            pass

        def with_structured_output(self, schema):
            class R:
                def invoke(_, msgs):
                    attempts.append(1)
                    raise ConnectionError("503")
            return R()
    mod = types.ModuleType("langchain_fireworks")
    mod.ChatFireworks = Chat
    monkeypatch.setitem(sys.modules, "langchain_fireworks", mod)
    c = FireworksClient(cache_dir=tmp_path)
    with pytest.raises(LLMTransportError):
        c.structured("s", "u", Pick1)
    assert len(attempts) == 3 and list(tmp_path.glob("*")) == []     # nothing cached on failure
    # a torn cache file is a miss, not a crash
    from e2ekg.llm import MODEL_ID, request_key
    (tmp_path / f"{request_key(MODEL_ID, 's', 'u', Pick1)}.json").write_text("{not json")

    class Ok:
        def invoke(self, msgs):
            return Pick1(choice="x")
    monkeypatch.setattr(FireworksClient, "_structured_chat", lambda self, schema: Ok())
    assert c.structured("s", "u", Pick1).choice == "x"
    assert not list(tmp_path.glob("*.tmp"))
