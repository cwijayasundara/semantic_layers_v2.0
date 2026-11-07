"""Realistic log templates for render_logs: a JVM-service bank plus extras per
app type, nginx routes for the edge, and the planted (non-background) lines.

A template is (level, component, text). `text` placeholders are filled from
three per-line random ints and the app context, see PLACEHOLDERS."""
from collections.abc import Callable

Template = tuple[str, str, str]

GENERIC: tuple[Template, ...] = (
    ("INFO", "server.RequestHandler", "Handled {rpc} in {ms}ms"),
    ("INFO", "server.RequestHandler", "Request completed status=OK rpc={rpc} latency={ms}ms"),
    ("DEBUG", "cache.LocalCache", "Cache hit for key {key}"),
    ("DEBUG", "cache.LocalCache", "Cache miss for key {key}, loading from {dep}"),
    ("INFO", "config.FastPropertyListener", "Refreshed fast properties ({n} changed)"),
    ("INFO", "discovery.EurekaClient", "Eureka heartbeat sent, status=UP"),
    ("INFO", "resilience.CircuitBreaker", "Circuit {dep} CLOSED after {k} successful probes"),
    ("WARN", "client.UpstreamClient", "Slow call to {dep}.{rpc}: {slow}ms"),
    ("INFO", "jvm.GcMonitor", "GC pause (G1 Evacuation Pause) {k}ms"),
    ("INFO", "grpc.ChannelPool", "Connection established to {dep} ({region})"),
    ("DEBUG", "server.ResponseWriter", "Serialized response of {bytes} bytes"),
    ("INFO", "batch.BatchProcessor", "Processed batch of {n} records in {ms}ms"),
    ("WARN", "client.UpstreamClient", "Retrying {dep}.{rpc} (attempt {k3}/3)"),
    ("INFO", "metrics.AtlasPublisher", "Published {n} series to atlas"),
    ("DEBUG", "member.ProfileResolver", "Resolved member {member} to profile {key}"),
    ("INFO", "server.RequestHandler", "Loaded {n} titles for member {member}"),
    ("ERROR", "codec.PayloadDecoder",
     "Failed to deserialize payload from {dep}: unexpected end of stream"),
    ("WARN", "server.Admission", "Request queue depth {n} above soft limit"),
    ("INFO", "jobs.Scheduler", "Scheduled task refresh-{key} completed in {ms}ms"),
    ("DEBUG", "grpc.ChannelPool", "gRPC channel to {dep} state=READY"),
    ("INFO", "health.HealthCheck", "Health check OK ({k} checks)"),
    ("WARN", "server.RequestHandler", "Deprecated API {rpc} called by {caller}"),
    ("INFO", "flags.FeatureFlags", "Evaluated {n} feature flags in {ms}ms"),
    ("DEBUG", "server.RequestHandler", "Fetching metadata for title {title}"),
    ("INFO", "auth.MetatronClient", "Service token refreshed, expires in {k}h"),
    ("ERROR", "client.UpstreamClient", "Timeout waiting for {dep} after {slow}ms"),
    ("INFO", "server.ThreadPoolStats", "Thread pool stats active={k} queued={n}"),
    ("WARN", "resilience.Fallback", "Fallback served for {rpc}"),
    ("DEBUG", "server.ResponseWriter", "Encoded response gzip ratio {pct}%"),
    ("INFO", "config.ArchaiusPoller", "Archaius poll complete: {n} properties"),
    ("ERROR", "client.UpstreamClient",
     "io.grpc.StatusRuntimeException: UNAVAILABLE: io exception from {dep}"),
    ("INFO", "cache.LocalCache", "Warmed cache with {n} entries"),
    ("DEBUG", "tracing.SpanExporter", "Exported span batch size={n}"),
    ("WARN", "jvm.ClockMonitor", "Clock drift {k}ms vs NTP"),
    ("INFO", "server.RequestHandler", "Received {rpc} from {caller}"),
    ("INFO", "client.UpstreamClient", "Sending {rpc} to {dep}"),
    ("DEBUG", "resilience.Bulkhead", "Bulkhead permits available={k}"),
    ("INFO", "cluster.ShardManager", "Shard rebalance complete ({k} shards)"),
    ("WARN", "server.ResponseWriter", "Payload size {bytes} bytes exceeds recommended limit"),
    ("INFO", "i18n.LocaleResolver", "Locale fallback en-US for member {member}"),
    ("INFO", "i18n.LocaleResolver", "Localized title missing for locale {locale}: {title_name}"),
    ("DEBUG", "server.RequestHandler", "ETag match for title {title}"),
    ("INFO", "ratelimit.TokenBucket", "Token bucket refilled ({n} tokens)"),
    ("ERROR", "server.RequestHandler",
     "Unhandled exception in {rpc}: java.lang.IllegalStateException: stream closed"),
)
EXTRA: dict[str, tuple[Template, ...]] = {
    "graphql": (
        ("INFO", "graphql.Executor", "Executed query HomePage in {ms}ms ({n} fields)"),
        ("INFO", "graphql.DataLoader", "DataLoader batch to {dep} size={n}"),
        ("DEBUG", "graphql.PersistedQueries", "Persisted query {key} cache hit"),
        ("WARN", "graphql.Executor", "Query complexity {n} close to limit 1000"),
        ("INFO", "graphql.SchemaRegistry", "Schema version {k} active"),
        ("DEBUG", "graphql.Executor", "Resolved field lolomo.rows in {ms}ms"),
    ),
    "datastore": (
        ("INFO", "db.compaction.CompactionTask", "Compacted {k} sstables to [{key}]"),
        ("INFO", "db.ColumnFamilyStore", "Flushing memtable for titles.by_id ({bytes} bytes)"),
        ("INFO", "service.GCInspector", "G1 Young Generation GC in {ms}ms"),
        ("INFO", "gms.Gossiper", "Node /10.0.{k}.{k3} state jump to NORMAL"),
        ("WARN", "db.ReadCommand", "Read {n} live rows and {k} tombstone cells for query"),
        ("INFO", "db.commitlog.CommitLog", "Commit log segment {key} recycled"),
        ("DEBUG", "service.StorageProxy", "Read repair on titles.by_id key {title}"),
    ),
    "cache": (
        ("INFO", "evcache.EVCacheClient", "get hit ratio {pct}% over last 60s"),
        ("INFO", "evcache.SlabMonitor", "Slab class {k} has {n} items"),
        ("DEBUG", "evcache.EVCacheClient", "set key {key} ttl=86400"),
        ("WARN", "evcache.EVCacheClient", "Replica zone {region} slow: {ms}ms"),
        ("INFO", "evcache.ConnectionMonitor", "{n} active client connections"),
    ),
    "gateway": (
        ("INFO", "filters.RouteFilter", "Routed {rpc} to {dep} in {ms}ms"),
        ("INFO", "filters.AuthFilter", "Authenticated request for member {member}"),
        ("WARN", "filters.RateLimitFilter", "Rate limit near threshold for {caller}"),
        ("DEBUG", "filters.HeaderFilter", "Stripped {k} hop-by-hop headers"),
    ),
}
LOCALES = ("pt-BR", "es-MX", "fr-FR", "de-DE", "sv-SE")
TITLE_NAMES = ("Amélie", "Élite", "Señora Acero", "La Reina del Sur", "Ångström", "Dark",
               "Lupin", "Narcos")
THREADS = ("grpc-default-executor-{k}", "main", "pool-3-thread-{k}", "nioEventLoopGroup-3-{k}")


def _key(a, b, c, x) -> str:
    return f"{c:08x}"


PLACEHOLDERS: dict[str, Callable] = {
    "ms": lambda a, b, c, x: a % 250 + 2,
    "slow": lambda a, b, c, x: 800 + a % 2200,
    "n": lambda a, b, c, x: b % 400 + 1,
    "k": lambda a, b, c, x: b % 31 + 1,
    "k3": lambda a, b, c, x: b % 3 + 1,
    "pct": lambda a, b, c, x: b % 100,
    "key": _key,
    "member": lambda a, b, c, x: 10_000_000 + c % 90_000_000,
    "title": lambda a, b, c, x: 80_000_000 + (b * 7919) % 1_000_000,
    "bytes": lambda a, b, c, x: a % 60_000 + 200,
    "dep": lambda a, b, c, x: x["deps"][b % len(x["deps"])],
    "caller": lambda a, b, c, x: x["callers"][c % len(x["callers"])],
    "rpc": lambda a, b, c, x: x["rpcs"][a % len(x["rpcs"])],
    "region": lambda a, b, c, x: x["region"],
    "locale": lambda a, b, c, x: LOCALES[a % len(LOCALES)],
    "title_name": lambda a, b, c, x: TITLE_NAMES[b % len(TITLE_NAMES)],
}

# Edge access-log "templates": (method, path, status, median bytes, median rt s, home route)
ROUTES: tuple[tuple[str, str, int, int, float, bool], ...] = (
    ("POST", "/graphql?op=HomePage", 200, 48_000, 0.120, True),
    ("POST", "/graphql?op=Lolomo", 200, 36_000, 0.100, True),
    ("GET", "/api/playback/manifest/{title}", 200, 9_000, 0.090, False),
    ("POST", "/api/logblob", 204, 0, 0.010, False),
    ("GET", "/api/profiles/{member}", 200, 2_400, 0.030, False),
    ("POST", "/graphql?op=TitleDetails", 200, 12_000, 0.080, True),
    ("GET", "/api/search?q={key}", 200, 6_500, 0.150, False),
    ("GET", "/healthcheck", 200, 15, 0.002, False),
    ("POST", "/graphql?op=ContinueWatching", 200, 8_000, 0.090, True),
    ("GET", "/api/artwork/{title}", 200, 32_000, 0.040, False),
    ("POST", "/api/playback/license", 200, 1_800, 0.060, False),
    ("GET", "/api/playback/manifest/{title}", 304, 0, 0.020, False),
    ("POST", "/graphql?op=HomePage", 401, 120, 0.004, False),
    ("GET", "/api/profiles/{member}", 404, 80, 0.006, False),
    ("POST", "/api/events/bookmark", 202, 0, 0.012, False),
    ("GET", "/api/ab/allocations", 200, 900, 0.015, False),
    ("POST", "/graphql?op=Billboard", 200, 15_000, 0.090, True),
    ("GET", "/robots.txt", 200, 68, 0.001, False),
    ("POST", "/api/ftl/probe", 200, 40, 0.003, False),
    ("GET", "/api/search/suggest?q={key}", 200, 1_200, 0.050, False),
    ("POST", "/graphql?op=MyList", 200, 7_000, 0.070, True),
    ("GET", "/api/playback/heartbeat", 204, 0, 0.008, False),
    ("POST", "/api/cl2/batch", 200, 300, 0.020, False),
    ("GET", "/favicon.ico", 200, 1_150, 0.001, False),
    ("POST", "/graphql?op=GenreRow", 200, 11_000, 0.085, True),
    ("GET", "/api/membership/status", 200, 600, 0.025, False),
    ("GET", "/api/playback/manifest/{title}", 403, 120, 0.010, False),
    ("POST", "/api/nq/msl", 200, 2_000, 0.030, False),
    ("POST", "/graphql?op=PreviewVideos", 200, 9_500, 0.075, True),
    ("GET", "/api/locale", 200, 350, 0.004, False),
    ("POST", "/api/search/log", 204, 0, 0.006, False),
    ("GET", "/api/profiles/{member}/avatar", 200, 22_000, 0.020, False),
    ("POST", "/graphql?op=Trailers", 200, 6_000, 0.060, True),
    ("GET", "/api/device/config", 200, 3_200, 0.012, False),
    ("POST", "/api/logout", 302, 0, 0.010, False),
    ("GET", "/api/playback/manifest/{title}", 500, 200, 0.300, False),
)
USER_AGENTS = {
    "tv": "Netflix/7.3.1 (TV; Tizen 6.5; Samsung QN90)",
    "android": "Netflix/{version} (Android 14; Pixel 8)",
    "ios": "Netflix/16.42.0 (iPhone; iOS 18.1)",
    "web": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 Safari/17.6",
}
ANDROID_PREVIOUS = "8.111.3"

# ---- planted lines -------------------------------------------------------
NPE_MSG = ('Error rendering artwork for title {title}: java.lang.NullPointerException: '
           'Cannot invoke "com.netflix.artwork.ImageSpec.getWidth()" because "spec" is null')
NPE_STACK = ("java.lang.NullPointerException: Cannot invoke "
             '"com.netflix.artwork.ImageSpec.getWidth()" because "spec" is null',
             "\tat com.netflix.artwork.render.Cropper.crop(Cropper.java:57)",
             "\tat com.netflix.artwork.render.ArtworkRenderer.render(ArtworkRenderer.java:112)",
             "\tat com.netflix.artwork.server.ArtworkHandler.getArtwork(ArtworkHandler.java:64)")
DEPLOY_BLIP: tuple[Template, ...] = (
    ("ERROR", "client.UpstreamClient",
     "Connection refused to {dep} during instance warm-up; retrying"),
    ("ERROR", "health.HealthCheck", "Health check failed: dependency {dep} not ready"),
    ("WARN", "discovery.EurekaClient", "Instance registered as STARTING, traffic not yet routed"),
    ("ERROR", "server.RequestHandler",
     "503 returned for {rpc}: upstream {dep} has no healthy hosts"),
)
FAULT_LINES: dict[str, tuple[Template, ...]] = {
    "capacity_reduction": (
        ("WARN", "server.Admission", "Request queue depth {n} exceeds soft limit 200"),
        ("WARN", "server.ThreadPoolStats",
         "Thread pool saturated: rejected execution (pool=grpc-server, active=200, queue=1000)"),
    ),
    "bad_deploy": (
        ("ERROR", "server.RequestHandler", "Unhandled exception in {rpc}: java.lang."
         "ClassCastException: class LolomoRow cannot be cast to class ListRow"),
    ),
    "cache_eviction_storm": (
        ("WARN", "evcache.SlabMonitor", "Evicted {n} items from slab class {k} (ttl expired)"),
    ),
    "db_hot_partition": (
        ("WARN", "db.ReadCommand",
         "Read timeout on titles.by_region partition key us-west-2 after {slow}ms"),
    ),
    "cert_expiry": (
        ("ERROR", "net.TlsServer", "TLS handshake failed: javax.net.ssl.SSLHandshakeException: "
         "PKIX path validation failed: validity check failed (certificate expired)"),
    ),
    "retry_storm": (
        ("WARN", "client.UpstreamClient", "Retrying {rpc} on {dep} (attempt {k3}/5, backoff 0ms)"),
    ),
}
