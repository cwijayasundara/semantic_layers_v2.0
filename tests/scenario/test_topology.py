import re

from helpers import rng, shortest_hops

from e2ekg.scenario.topology import (
    downstream_closure, hero_catalog, hero_edges, upstream_closure,
)

NAMED = ("client-tv client-android client-ios client-web edge-gateway "
         "homepage-graphql lolomo-svc row-ranker title-batch-loader "
         "title-metadata-svc artwork-svc artwork-metadata-svc entitlement-svc "
         "api-gateway playback-api ab-allocator evcache-titles cassandra-titles "
         "ratings-svc profile-svc search-svc").split()


def edges():
    return hero_edges(hero_catalog(rng(7)))


def test_catalog_150_unique_kebab_and_named():
    cat = hero_catalog(rng(7))
    ids = [a.id for a in cat]
    assert len(cat) == 150 and len(set(ids)) == 150
    assert set(NAMED) <= set(ids)
    assert all(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", i) for i in ids)


def test_catalog_deterministic():
    assert hero_catalog(rng(7)) == hero_catalog(rng(7))


def test_api_gateway_owner_region_and_aliases():
    by = {a.id: a for a in hero_catalog(rng(7))}
    assert by["api-gateway"].owner_team == "Team Bedrock"
    assert "us-east-1" in by["api-gateway"].regions
    assert "TitleMetadataSvc" in by["title-metadata-svc"].aliases
    assert all("TMS" not in a.aliases for a in by.values())


def test_hero_path_is_six_hops():
    assert shortest_hops(edges(), "client-tv", "title-metadata-svc") == 6
    for c in ("client-android", "client-ios", "client-web"):
        assert shortest_hops(edges(), c, "title-metadata-svc") == 6


def test_pool_size_and_named_edges():
    e = {(x.caller, x.callee): x for x in edges()}
    assert e[("title-batch-loader", "title-metadata-svc")].pool_size == 64
    assert ("row-ranker", "ab-allocator") in e
    assert ("lolomo-svc", "artwork-svc") in e


def test_api_gateway_not_downstream_of_homepage():
    e = edges()
    assert "api-gateway" not in downstream_closure(e, "homepage-graphql")
    assert "api-gateway" in downstream_closure(e, "playback-api")
    assert "api-gateway" in downstream_closure(e, "client-tv")
    assert "playback-api" not in downstream_closure(e, "homepage-graphql")


def test_closures_transitive_and_exclude_self():
    e = edges()
    d = downstream_closure(e, "lolomo-svc")
    assert {"row-ranker", "title-batch-loader", "title-metadata-svc"} <= d
    assert "lolomo-svc" not in d
    u = upstream_closure(e, "title-metadata-svc")
    assert {"title-batch-loader", "homepage-graphql", "edge-gateway",
            "client-tv", "client-web"} <= u
    assert "title-metadata-svc" not in u


def test_acyclic_and_degree_caps_and_filler_edges():
    e = edges()
    ids = {a.id for a in hero_catalog(rng(7))}
    assert all(x.caller in ids and x.callee in ids for x in e)
    for a in ids:
        assert a not in downstream_closure(e, a)
    from collections import Counter
    assert max(Counter(x.caller for x in e).values()) <= 50
    assert max(Counter(x.callee for x in e).values()) <= 50
    named = set(NAMED)
    fillers = ids - named
    assert all(any(x.caller == f for x in e) or any(x.callee == f for x in e)
               for f in fillers)
    assert any(x.caller in named and x.callee in fillers for x in e)
    assert not any(x.caller in fillers and x.callee in named for x in e)
