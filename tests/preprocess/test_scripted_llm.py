"""The ScriptedClient stands in for the extraction model (R7), so these pin behaviour a model
has: it reads past a typo, surfaces an acronym as a service mention, and attributes a
bare reply to the single service its thread is about. The message shapes are the hero
channel's (generate/hero_chat.py); the typos are what generate/mess/chat.py injects."""
import json

import pytest
from pp_helpers import app_uri

from e2ekg.generate.hero_chat import BEATS
from e2ekg.preprocess.chat_extract import BatchExtraction
from e2ekg.scripted_llm import ScriptedClient

PROPOSAL = "can we bump min to 40 on the TMS ASG and let it scale back out?"


def run(*texts: str, parents: dict[int, int] | None = None) -> BatchExtraction:
    items = [{"i": n, "user": f"U{n}", "ts": f"{n}", "text": t,
              "thread_parent": (parents or {}).get(n)} for n, t in enumerate(texts)]
    return ScriptedClient().structured("", json.dumps({"messages": items}), BatchExtraction)


def swaps(text: str):
    """Every text chat.typo can produce: two adjacent letters swapped inside one word of 4+
    letters, never the first letter or the last two."""
    pos = 0
    for w in text.split(" "):
        if len(w) >= 4 and w.isalpha():
            for j in range(1, len(w) - 2):
                if w[j] != w[j + 1]:
                    p = pos + j
                    yield text[:p] + text[p + 1] + text[p] + text[p + 2:]
        pos += len(w) + 1


def shape(out: BatchExtraction):
    return [(m.label.category, m.label.sentiment, [c.kind for c in m.claims])
            for m in out.messages]


def test_the_proposal_with_a_typo_in_bump_is_still_a_mitigation_about_tms():
    out = run("can we bmup min to 40 on the TMS ASG and let it scale back out?")
    (claim,) = out.messages[0].claims
    assert claim.kind == "mitigation" and claim.about_surface == ["TMS"]


@pytest.mark.parametrize("text", [b[4] for b in BEATS], ids=lambda t: t[:40])
def test_a_typo_the_injector_makes_does_not_change_what_is_extracted(text):
    """The typo hits any word; a model reads through it, so the claims, category and
    sentiment of the hero channel's own messages hold for every possible swap. A typo inside a
    service name is a different matter (names are not words), so those words are skipped."""
    base = shape(run(text))
    for typo in swaps(text):
        got = shape(run(typo))
        assert got == base, f"{typo!r}"


def test_a_short_uppercase_token_is_a_service_mention():
    out = run("RRK p99 is up across the board", "HPG partial responses on the lolomo field")
    assert out.messages[0].claims[0].about_surface == ["RRK"]
    assert out.messages[1].claims[0].about_surface == ["HPG"]


def test_generic_infrastructure_acronyms_are_not_services():
    out = run("TTR is up, CPU per instance is pegged on the ASG, NPE in the logs")
    assert all(c.about_surface == [] for c in out.messages[0].claims)


def test_a_bare_reply_is_about_the_one_service_its_thread_is_about():
    out = run(PROPOSAL, "yes, do it. bump min to 40 now, we'll figure out why after",
              "bump min to 40 submitted on the ASG, instances launching",
              parents={1: 0, 2: 0})
    assert out.messages[1].claims[-1].about_surface == ["TMS"]
    assert out.messages[2].claims[0].about_surface == ["TMS"]


def test_a_reply_under_a_parent_naming_several_services_inherits_nothing():
    out = run("so far: TMS slow -> title-batch-loader pool exhausted -> row-ranker deadlines",
              "bump min to 40 now", parents={1: 0})
    assert out.messages[1].claims[0].about_surface == []


def test_a_reply_naming_its_own_service_keeps_it_not_the_threads():
    out = run("api-gateway deployed at 15:18, that lines up pretty well",
              "starting a rollback of the TMS ASG", parents={1: 0})
    assert out.messages[1].claims[0].about_surface == ["TMS"]


def test_a_herring_thread_gains_no_claim_it_did_not_have():
    """Inheriting an app attributes existing claims, it never creates claims: chatter in a
    herring app's thread stays claim-free."""
    out = run("api-gateway deployed at 15:18, that lines up pretty well",
              "+1, seeing it on web as well", "ack", "any update?", parents={1: 0, 2: 0, 3: 0})
    assert [len(m.claims) for m in out.messages[1:]] == [0, 0, 0]


def test_the_hero_proposal_chain_reaches_tms_end_to_end(preprocessed_hero, scripted_llm):
    import polars as pl
    from pp_helpers import table

    from e2ekg.preprocess.chat_extract import extract_chat
    from e2ekg.preprocess.identity import Resolver, catalog_from_table
    slack = table(preprocessed_hero, "slack")
    res = Resolver(catalog_from_table(table(preprocessed_hero, "catalog")), scripted_llm)
    msgs, claims = extract_chat(slack, res, scripted_llm)
    tms = claims.filter((pl.col("kind") == "mitigation")
                        & (pl.col("about_app_uri") == app_uri("title-metadata-svc")))
    txt = slack.select("raw_id", "text", "ts_utc")
    got = tms.join(txt, on="raw_id").sort("ts_utc")
    assert got["text"][0].startswith("can we b") and "min to 40" in got["text"][0]
    assert any(t.startswith("bump min to 40 submitted") for t in got["text"])
    assert any(t.startswith("yes, do it") for t in got["text"])


def test_a_typo_in_the_proposal_still_makes_it_the_first_tms_mitigation(scripted_llm):
    """CQ05's shape at full scale: the injector typo'd "bump" in the proposal, and the two
    bare replies under it name no service."""
    from datetime import UTC, datetime

    import polars as pl

    from e2ekg.preprocess.chat_extract import extract_chat
    from e2ekg.preprocess.identity import Resolver
    from e2ekg.scenario.model import Application
    ch = "inc-5377-homepage-ttr"
    texts = [("1000.000001", None, "TMS CPU per instance is pegged. maybe there aren't enough boxes?"),
             ("1030.986921", None, "can we bmup min to 40 on the TMS ASG and let it scale back out?"),
             ("1070.031042", "1030.986921", "yes, do it. bump min to 40 now, we'll figure out why after"),
             ("1120.153017", "1030.986921", "bump min to 40 submitted on the ASG, instances launching"),
             ("1130.000001", None, "comms posted a holding statement for members on all four platforms")]
    rows = [{"raw_id": f"chat|slack/{ch}/m.jsonl|{i * 100}|9|{i:016x}", "source": "chat",
             "rel_path": f"slack/{ch}/m.jsonl", "ts_raw": ts, "channel": ch, "user": f"U{i}",
             "text": t, "subtype": None, "thread_ts": th, "fields": "{}",
             "ts_utc": datetime.fromtimestamp(int(ts.split(".")[0]), UTC).replace(
                 microsecond=int(ts.split(".")[1]))} for i, (ts, th, t) in enumerate(texts)]
    df = pl.DataFrame(rows).with_columns(pl.col("ts_utc").dt.replace_time_zone("UTC"))
    apps = tuple(Application(id=i, aliases=(), owner_team="T", tier=1, regions=(), kind="service")
                 for i in ("title-metadata-svc", "api-gateway"))
    _, claims = extract_chat(df, Resolver(apps, scripted_llm), scripted_llm)
    tms = claims.filter((pl.col("kind") == "mitigation")
                        & (pl.col("about_app_uri") == app_uri("title-metadata-svc")))
    texts_of = df.select("raw_id", "text").join(tms, on="raw_id").sort("raw_id")["text"].to_list()
    assert texts_of[0].startswith("can we bmup min to 40")
    assert len(texts_of) == 3
    holding = claims.filter(pl.col("raw_id") == df["raw_id"][4])
    assert holding["about_app_uri"].is_null().all()
