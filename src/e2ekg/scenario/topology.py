"""Hero service topology: a 150-app catalog and its dependency edges."""
from collections import deque

import numpy as np

from e2ekg.scenario.model import AppId, Application, DependencyEdge

MAX_DEGREE = 50

# id -> (kind, tier, owner_team, regions, aliases)
_W2, _E1 = ("us-west-2",), ("us-east-1",)
_BOTH = ("us-west-2", "us-east-1")
_NAMED: dict[str, tuple] = {
    "client-tv": ("client", 1, "Team Devices", _BOTH, ("ClientTV", "tv-client")),
    "client-android": ("client", 1, "Team Devices", _BOTH, ("ClientAndroid", "android-app")),
    "client-ios": ("client", 1, "Team Devices", _BOTH, ("ClientIOS", "ios-app")),
    "client-web": ("client", 1, "Team Devices", _BOTH, ("ClientWeb", "web-app")),
    "edge-gateway": ("edge", 0, "Team Edge", _BOTH, ("EdgeGateway", "zuul-edge")),
    "homepage-graphql": ("service", 0, "Team Discovery", _W2, ("HomepageGraphQL", "homepage-gql")),
    "lolomo-svc": ("service", 0, "Team Discovery", _W2, ("LolomoSvc", "lolomo")),
    "row-ranker": ("service", 1, "Team Personalization", _W2, ("RowRanker", "rowranker")),
    "title-batch-loader": ("service", 1, "Team Content Platform", _W2, ("TitleBatchLoader", "titlebatchloader")),
    "title-metadata-svc": ("service", 0, "Team Content Platform", _W2, ("TitleMetadataSvc", "titlemetadata")),
    "artwork-svc": ("service", 1, "Team Imagery", _W2, ("ArtworkSvc", "artwork")),
    "artwork-metadata-svc": ("service", 2, "Team Imagery", _W2, ("ArtworkMetadataSvc", "artworkmeta")),
    "entitlement-svc": ("service", 0, "Team Membership", _BOTH, ("EntitlementSvc", "entitlements")),
    "api-gateway": ("edge", 0, "Team Bedrock", _BOTH, ("ApiGateway", "api-gw")),
    "playback-api": ("service", 0, "Team Playback", _BOTH, ("PlaybackApi", "playbackapi")),
    "ab-allocator": ("service", 1, "Team Experimentation", _W2, ("AbAllocator", "aballoc")),
    "evcache-titles": ("cache", 1, "Team Caching", _W2, ("EVCacheTitles", "evcache-title")),
    "cassandra-titles": ("datastore", 0, "Team Data Platform", _W2, ("CassandraTitles", "cass-titles")),
    "ratings-svc": ("service", 2, "Team Discovery", _W2, ("RatingsSvc", "ratings")),
    "profile-svc": ("service", 1, "Team Membership", _BOTH, ("ProfileSvc", "profiles")),
    "search-svc": ("service", 1, "Team Search", _W2, ("SearchSvc", "search")),
}
_N_FILLER = 150 - len(_NAMED)

_PREFIX = ("billing", "catalog", "cohort", "device", "encode", "feed", "geo",
           "graph", "inbox", "ingest", "label", "locale", "media", "notify",
           "offer", "packager", "pricing", "promo", "queue", "quota", "recs",
           "render", "review", "schedule", "session", "subtitle", "sync",
           "taste", "token", "trailer", "usage", "vault", "voice", "widget")
_SUFFIX = ("svc", "api", "worker", "proxy", "indexer", "router", "store", "db",
           "cache", "manager", "resolver", "pipeline")
_TEAMS = ("Team Discovery", "Team Playback", "Team Membership", "Team Imagery",
          "Team Data Platform", "Team Caching", "Team Streaming", "Team Studio",
          "Team Ads", "Team Security", "Team Insights", "Team Localization")
_REGION_SETS = (_W2, _E1, _BOTH)


def hero_catalog(rng: np.random.Generator) -> tuple[Application, ...]:
    apps = [Application(id=AppId(i), aliases=v[4], owner_team=v[2], tier=v[1],
                        regions=v[3], kind=v[0]) for i, v in _NAMED.items()]
    names: set[str] = set(_NAMED)
    while len(apps) < 150:
        name = f"{_PREFIX[rng.integers(len(_PREFIX))]}-{_SUFFIX[rng.integers(len(_SUFFIX))]}"
        if name in names:
            name = f"{name}-{int(rng.integers(2, 100))}"
            if name in names:
                continue
        names.add(name)
        suffix = name.split("-")[1]
        kind = ("datastore" if suffix in ("db", "store") else
                "cache" if suffix == "cache" else "service")
        apps.append(Application(
            id=AppId(name), aliases=(), kind=kind,
            owner_team=_TEAMS[rng.integers(len(_TEAMS))],
            tier=int(rng.integers(1, 4)),
            regions=_REGION_SETS[rng.integers(len(_REGION_SETS))]))
    return tuple(apps)


def _e(caller: str, callee: str, protocol: str, pool: int | None = None) -> DependencyEdge:
    return DependencyEdge(caller=AppId(caller), callee=AppId(callee),
                          protocol=protocol, pool_size=pool)


def hero_edges(catalog: tuple[Application, ...]) -> tuple[DependencyEdge, ...]:
    """Deterministic edges. Fillers only call later fillers (a DAG), and no
    filler ever calls a named app, so nothing reaches api-gateway except via
    playback-api."""
    edges = [_e(c, "edge-gateway", "http")
             for c in ("client-tv", "client-android", "client-ios", "client-web")]
    edges += [
        _e("edge-gateway", "homepage-graphql", "http"),
        _e("edge-gateway", "playback-api", "http"),
        _e("edge-gateway", "search-svc", "grpc"),
        _e("homepage-graphql", "lolomo-svc", "graphql"),
        _e("homepage-graphql", "profile-svc", "grpc"),
        _e("lolomo-svc", "row-ranker", "grpc"),
        _e("lolomo-svc", "artwork-svc", "grpc"),
        _e("lolomo-svc", "ratings-svc", "grpc"),
        _e("row-ranker", "title-batch-loader", "grpc"),
        _e("row-ranker", "ab-allocator", "grpc"),
        _e("title-batch-loader", "title-metadata-svc", "grpc", 64),
        _e("title-metadata-svc", "evcache-titles", "memcached"),
        _e("title-metadata-svc", "cassandra-titles", "cql"),
        _e("artwork-svc", "artwork-metadata-svc", "grpc"),
        _e("playback-api", "api-gateway", "http"),
        _e("playback-api", "entitlement-svc", "grpc"),
        _e("playback-api", "ab-allocator", "grpc"),
        _e("api-gateway", "entitlement-svc", "grpc"),
        _e("api-gateway", "profile-svc", "grpc"),
    ]
    fillers = [a for a in catalog if a.id not in _NAMED]
    out_n: dict[str, int] = {}
    in_n: dict[str, int] = {}
    for x in edges:
        out_n[x.caller] = out_n.get(x.caller, 0) + 1
        in_n[x.callee] = in_n.get(x.callee, 0) + 1
    have = {(x.caller, x.callee) for x in edges}
    r = np.random.default_rng(20261014)  # fixed: hero_edges takes no rng

    def add(caller: str, callee: str, proto: str) -> None:
        if ((caller, callee) in have or out_n.get(caller, 0) >= MAX_DEGREE
                or in_n.get(callee, 0) >= MAX_DEGREE):
            return
        have.add((caller, callee))
        out_n[caller] = out_n.get(caller, 0) + 1
        in_n[callee] = in_n.get(callee, 0) + 1
        edges.append(_e(caller, callee, proto))

    def proto_for(callee: Application) -> str:
        return {"datastore": "cql", "cache": "memcached"}.get(callee.kind, "grpc")

    # Named, non-hero-chain services call into a few fillers.
    for caller in ("artwork-metadata-svc", "ratings-svc", "profile-svc",
                   "search-svc", "entitlement-svc", "ab-allocator"):
        for j in r.choice(len(fillers), size=3, replace=False):
            add(caller, fillers[j].id, proto_for(fillers[j]))
    # Fillers call 1-4 later fillers (datastores and caches included).
    for i, f in enumerate(fillers[:-1]):
        if f.kind in ("datastore", "cache"):
            continue
        later = fillers[i + 1:]
        k = min(int(r.integers(1, 5)), len(later))
        for j in r.choice(len(later), size=k, replace=False):
            add(f.id, later[j].id, proto_for(later[j]))
    # Guarantee every filler is connected.
    linked = {x.caller for x in edges} | {x.callee for x in edges}
    for i, f in enumerate(fillers):
        if f.id not in linked:
            add("search-svc", f.id, proto_for(f))
    return tuple(edges)


def _closure(edges, app: AppId, forward: bool) -> frozenset[AppId]:
    nxt: dict[str, list[str]] = {}
    for e in edges:
        a, b = (e.caller, e.callee) if forward else (e.callee, e.caller)
        nxt.setdefault(a, []).append(b)
    seen: set[str] = set()
    q = deque([app])
    while q:
        for m in nxt.get(q.popleft(), ()):
            if m not in seen:
                seen.add(m)
                q.append(m)
    seen.discard(app)
    return frozenset(AppId(s) for s in seen)


def downstream_closure(edges, app: AppId) -> frozenset[AppId]:
    """Everything transitively reachable via caller->callee, excluding app."""
    return _closure(edges, app, True)


def upstream_closure(edges, app: AppId) -> frozenset[AppId]:
    """All transitive callers of app, excluding app."""
    return _closure(edges, app, False)
