import json

import polars as pl
import pytest
from pp_helpers import table

from e2ekg.llm import MODEL_ID, FakeClient, LLMContractError, request_key
from e2ekg.preprocess.identity import (
    SYSTEM,
    Pick,
    Resolver,
    catalog_from_table,
    llm_user_prompt,
)

TMS = "title-metadata-svc"


@pytest.fixture(scope="module")
def catalog(preprocessed_hero):
    return catalog_from_table(table(preprocessed_hero, "catalog"))


def key_for(resolver, surface, context=""):
    return request_key(MODEL_ID, SYSTEM, llm_user_prompt(resolver, surface, context), Pick)


@pytest.fixture(scope="module")
def resolver_no_llm(catalog):
    return Resolver(catalog, None)


@pytest.fixture
def resolver(catalog):
    base = Resolver(catalog, None)
    fake = FakeClient({key_for(base, "tms-prod-v042"): Pick(choice=TMS),
                       FakeClient.ANY_KEY: Pick(choice="none")})
    return Resolver(catalog, fake)


def test_catalog_has_named_apps(catalog):
    ids = {a.id for a in catalog}
    assert {TMS, "artwork-metadata-svc"} <= ids and len(ids) == 150


@pytest.mark.parametrize("surface", ["title-metadata-svc", "TitleMetadataSvc", "titlemetadata",
                                     "tms-prod-v042", "title-metadata-svc-7f9c2d4e1a",
                                     "Title-Metadata-Svc", "TITLE-METADATA-SVC",
                                     "title-metadata-svc-v042", "title-metadata-svc-prod",
                                     "title-metadata-svc-bcdfghjklm-nqrst"])
def test_surface_forms_resolve(resolver, surface):
    assert resolver.resolve(surface).app == TMS


def test_methods(resolver_no_llm):
    r = resolver_no_llm
    assert r.resolve("title-metadata-svc").method == "exact"
    assert r.resolve("TitleMetadataSvc").method == "alias"
    assert r.resolve("title-metadata-svc-prod").method == "pattern"
    assert r.resolve("schedule-indexer-2-prod-prod").app == "schedule-indexer-2"
    assert r.resolve("zuul-edge-v2").app == "edge-gateway"      # stale alias + -v2
    assert r.resolve("geo-resolver-76").app == "geo-resolver-76"  # numbered app, not a version


def test_ambiguous_alias_stays_unresolved(resolver_no_llm):
    r = resolver_no_llm.resolve("metadata")
    assert r.app is None and r.method == "unresolved"


def test_slack_only_nickname_needs_llm(catalog):
    base = Resolver(catalog, None)
    assert base.resolve("TMS", context="TMS pods scaled in").app is None
    ctx = "TMS pods scaled in"
    fake = FakeClient({key_for(base, "TMS", ctx): Pick(choice=TMS)})
    r = Resolver(catalog, fake).resolve("TMS", context=ctx)
    assert r.method == "llm" and r.app == TMS and len(fake.calls) == 1


def test_llm_cannot_invent_app(catalog):
    fake = FakeClient({FakeClient.ANY_KEY: Pick(choice="made-up-svc")})
    assert Resolver(catalog, fake).resolve("TMS").app is None


def test_llm_must_choose_from_closed_list(catalog):
    # a real catalog app that is not among the offered candidates is rejected
    fake = FakeClient({FakeClient.ANY_KEY: Pick(choice="billing-db")})
    assert Resolver(catalog, fake).resolve("TMS").app is None


def test_llm_error_is_unresolved(catalog):
    fake = FakeClient({FakeClient.ANY_KEY: LLMContractError("bad")})
    assert Resolver(catalog, fake).resolve("TMS").method == "unresolved"


def test_team_name_never_becomes_app(resolver):
    assert resolver.resolve("Team Bedrock").app is None


def test_team_name_skips_llm(catalog):
    fake = FakeClient({FakeClient.ANY_KEY: Pick(choice=TMS)})
    assert Resolver(catalog, fake).resolve("Team Bedrock").app is None
    assert fake.calls == []


# Names the generator emits from the full alias list while the rendered catalog keeps a
# shorter one (catalog drift). Without an LLM they are unresolved; with one, the true app
# must be on the closed candidate list.
DRIFT = {"homepage-gql": "homepage-graphql", "artworkmeta": "artwork-metadata-svc",
         "cass-titles": "cassandra-titles"}


def test_every_processed_surface_resolves_or_is_allowed(preprocessed_hero, resolver_no_llm):
    bad = {}
    for t in ("logs", "spans", "events", "alerts", "pages", "incidents", "metrics_prom"):
        s = table(preprocessed_hero, t)["surface_app"].drop_nulls().unique().to_list()
        miss = [x for x in s if resolver_no_llm.resolve(x).app is None]
        if miss:
            bad[t] = miss
    assert {x for v in bad.values() for x in v} <= set(DRIFT), bad


@pytest.mark.parametrize("surface,app", DRIFT.items())
def test_drift_names_reach_llm_candidate_list(resolver_no_llm, surface, app):
    r = resolver_no_llm
    core = surface
    assert app in r.candidates(core, "", r._fuzzy(core))
    fake = FakeClient({FakeClient.ANY_KEY: Pick(choice=app)})
    assert Resolver(tuple(_catalog_of(r)), fake).resolve(surface).app == app


def _catalog_of(r):
    from e2ekg.scenario.model import Application
    return [Application(id=i, aliases=(), owner_team="t", tier=0, regions=(), kind="service")
            for i in r.ids]


def test_resolve_pass_columns(preprocessed_hero):
    for t in ("logs", "spans", "metrics_atlas", "metrics_prom", "events", "catalog"):
        df = table(preprocessed_hero, t)
        assert {"app_uri", "app_method", "app_confidence", "surface_app"} <= set(df.columns)
        assert df["app_method"].null_count() == 0
    atlas = table(preprocessed_hero, "metrics_atlas")
    assert atlas["app_uri"].null_count() == 0            # the fixture ran with ScriptedClient
    drift = atlas.filter(pl.col("app_method") == "llm")  # catalog-drift names: closed list
    assert drift.height > 0 and all(
        any(f'"{d}"' in tg for d in DRIFT) for tg in drift["tags"])
    cat = table(preprocessed_hero, "catalog")
    assert (cat["app_method"] == "exact").all()
    sp = table(preprocessed_hero, "spans")
    assert (sp["app_method"] != "exact").any()          # suffix / alias forms exist
    ledger = json.loads((preprocessed_hero.proc / "ledger.json").read_text())
    assert ledger["identity"]["spans"]["pattern"] > 0
    assert pl.Series(sp["app_uri"]).drop_nulls().str.starts_with(
        "https://kg.local/app/application/").all()


def test_dir_derived_surface_is_flagged(preprocessed_hero):
    lg = table(preprocessed_hero, "logs")
    d = lg.filter(pl.col("log_format").is_in(["log4j", "nginx"]))
    assert d["app_from_dir"].all()
    assert not lg.filter(~pl.col("log_format").is_in(["log4j", "nginx"]))["app_from_dir"].any()


@pytest.mark.parametrize("surface,wrong", [("artwork-meta", "artwork-svc"),
                                           ("geo-db-10", "geo-db"),
                                           ("ratings-db", "ratings-svc"),
                                           ("profiles-db", "profile-svc")])
def test_subset_names_do_not_fuzzy_match(resolver_no_llm, surface, wrong):
    """token_set_ratio is 100 when the catalog name's tokens are a subset of the query's;
    a query token the name lacks must veto the match."""
    r = resolver_no_llm.resolve(surface)
    assert (r.app != wrong and r.method != "fuzzy") or r.app == "artwork-metadata-svc"


def test_abbreviated_token_resolves_to_the_app_that_covers_it(resolver_no_llm):
    r = resolver_no_llm.resolve("artwork-meta")
    assert (r.app, r.method) == ("artwork-metadata-svc", "fuzzy")
    assert resolver_no_llm.resolve("geo-db-10").app is None


def test_llm_prompt_carries_the_stripped_core(catalog):
    fake = FakeClient({FakeClient.ANY_KEY: Pick(choice="none")})
    Resolver(catalog, fake).resolve("tms-prod-v042")
    assert json.loads(fake.calls[0][2])["core"] == "tms"


def test_every_injected_surface_resolves_to_the_right_app(preprocessed_hero, catalog):
    """Oracle (tests only): for each id.* injection in the manifest the surface the
    generator wrote must resolve to the app it was written for, never to another app.
    Names the catalog does not carry resolve through the closed LLM list."""
    from fixtures.scripted_llm import ScriptedClient
    r = Resolver(catalog, ScriptedClient())
    seen = wrong = unresolved = 0
    for inj in preprocessed_hero_injections(preprocessed_hero):
        d = inj["detail"]
        if inj["injector_id"] == "id.slack_only_rename" or "surface" not in d or "app" not in d:
            continue
        seen += 1
        got = r.resolve(d["surface"]).app
        wrong += got is not None and got != d["app"]
        unresolved += got is None
    assert seen > 500 and wrong == 0 and unresolved == 0


def preprocessed_hero_injections(p):
    m = json.loads((p.answers / "hero.manifest.json").read_text())
    return [dict(i, detail=json.loads(i["detail"])) for i in m["injections"]
            if i["injector_id"].startswith("id.")]


def test_cli_llm_flag_builds_chat_tables(preprocessed_hero, tmp_path):
    import subprocess
    import sys
    out = tmp_path / "proc"
    base = [sys.executable, "-m", "e2ekg.cli", "preprocess", "--scenario", "hero", "--raw",
            str(preprocessed_hero.raw.parent), "--out", str(out)]
    p = subprocess.run([*base, "--llm", "scripted"], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert (out / "hero" / "derived" / "chat_messages.parquet").exists()
    p = subprocess.run([*base, "--llm", "none"], capture_output=True, text=True)
    assert p.returncode == 0 and not (out / "hero" / "derived" / "chat_messages.parquet").exists()


def test_cli_fireworks_needs_a_key(preprocessed_hero, tmp_path):
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k != "FIREWORKS_API_KEY"}
    p = subprocess.run([sys.executable, "-m", "e2ekg.cli", "preprocess", "--scenario", "hero",
                        "--raw", str(preprocessed_hero.raw.parent), "--out", str(tmp_path),
                        "--llm", "fireworks"], capture_output=True, text=True, env=env)
    assert p.returncode != 0 and "FIREWORKS_API_KEY" in p.stderr
