"""A small hand-built hero record set: every source and fmt, and enough of
each that every registered injector has something to corrupt."""
from datetime import timedelta

import pytest

from e2ekg.generate.records import RawRecord
from e2ekg.timeutil import pt

T0 = pt(2026, 10, 14, 14, 52)


def _t(sec: float):
    return T0 + timedelta(seconds=sec)


def _r(source, rel_path, fmt, fields, at, host=None, tags=()):
    return RawRecord(source=source, rel_path=rel_path, fmt=fmt, fields=fields,
                     at_utc=at, host=host, tags=frozenset(tags))


def _catalog():
    return [_r("catalog", "catalog/apps.yaml", "catalog_yaml",
               {"id": app, "aliases": list(al), "owner_team": team, "tier": 0,
                "regions": ["us-west-2"], "kind": "service"}, None)
            for app, al, team in (
                ("title-metadata-svc", ("TitleMetadataSvc", "titlemetadata"),
                 "Team Content Platform"),
                ("row-ranker", ("RowRanker", "rowranker"), "Team Personalization"))]


def _atlas():
    out = []
    for i in range(12):
        t = _t(60 * i)
        ev = ("evidence:metric_anomaly",) if i >= 6 else ()
        out.append(_r("atlas", "metrics/atlas.jsonl", "atlas_jsonl",
                      {"name": "title.latency.p99", "tags": {"nf.app": "title-metadata-svc",
                                                             "nf.region": "us-west-2"},
                       "t": t, "v": 120.0 + 40 * i}, t, tags=ev))
        out.append(_r("atlas", "metrics/atlas.jsonl", "atlas_jsonl",
                      {"name": "client.ttr.p90", "tags": {"nf.app": "client-tv", "platform": "tv",
                                                          "ab.cell": "A"},
                       "t": t, "v": 2.1 + (1.5 if i >= 8 else 0.0)}, t))
        out.append(_r("atlas", "metrics/atlas.jsonl", "atlas_jsonl",
                      {"name": "requests.rps", "tags": {"nf.app": "row-ranker"},
                       "t": t, "v": 900.0 + i}, t))
    return out


def _prom():
    out = []
    for k, hhmm in enumerate(("1450", "1455", "1500", "1505")):
        t = _t(300 * k)
        p = f"metrics/prom/title-metadata-svc-{hhmm}.prom"
        lines = [
            {"kind": "help", "name": "http_requests_total", "text": "Total HTTP requests."},
            {"kind": "type", "name": "http_requests_total", "type": "counter"},
            {"kind": "sample", "name": "http_requests_total",
             "labels": {"app": "title-metadata-svc", "instance": "i-0aa1"}, "value": 1000 + 500 * k,
             "t": t},
            {"kind": "help", "name": "request_latency_ms", "text": "Request latency in ms."},
            {"kind": "type", "name": "request_latency_ms", "type": "gauge"},
            {"kind": "sample", "name": "request_latency_ms",
             "labels": {"app": "title-metadata-svc", "instance": "i-0aa1"}, "value": 35.5 + k,
             "t": t},
        ]
        out += [_r("prom", p, "prom", f, t, host="i-0aa1") for f in lines]
    return out


def _logs():
    out = []
    for i in range(30):
        msg = "Título não encontrado: Amélie" if i % 7 == 0 else f"resolved rows for profile {i}"
        out.append(_r("logs", "logs/homepage-graphql/i-0hg1.log", "json",
                      {"ts": _t(i), "level": "INFO", "app": "homepage-graphql", "msg": msg,
                       "request_id": f"req-{i:04d}"}, _t(i), host="i-0hg1"))
    for i in range(10):
        out.append(_r("logs", "logs/row-ranker/i-0rr1.log", "logfmt",
                      {"ts": _t(2 * i), "level": "warn" if i > 5 else "info", "app": "row-ranker",
                       "msg": "upstream call slow", "upstream": "title-batch-loader",
                       "latency_ms": 100 * i}, _t(2 * i), host="i-0rr1"))
    for host in ("i-0tb1", "i-0tb2"):
        p = f"logs/title-batch-loader/{host}.log"
        for i in range(8):
            at = _t(3 * i)
            if i % 3 == 2:
                out.append(_r("logs", p, "log4j", {
                    "ts": at, "level": "ERROR", "thread": "grpc-worker-7",
                    "logger": "c.n.tbl.MetadataClient",
                    "msg": "Connection pool exhausted calling title-metadata-svc",
                    "stack": ["io.grpc.StatusRuntimeException: RESOURCE_EXHAUSTED: pool 64/64",
                              "\tat c.n.tbl.MetadataClient.fetch(MetadataClient.java:88)",
                              "\tat c.n.tbl.BatchLoader.load(BatchLoader.java:41)"]},
                    at, host=host, tags=("evidence:pool_exhaustion", "evidence:log_error")))
            else:
                out.append(_r("logs", p, "log4j", {
                    "ts": at, "level": "INFO", "thread": "main", "logger": "c.n.tbl.BatchLoader",
                    "msg": f"loaded batch {i}"}, at, host=host))
    for i in range(6):
        out.append(_r("logs", "logs/edge-gateway/i-0eg1.log", "nginx", {
            "remote_addr": f"10.0.3.{i}", "remote_user": "-", "ts": _t(i),
            "method": "GET", "path": "/graphql?q=home", "protocol": "HTTP/1.1",
            "status": 200 if i < 4 else 504, "bytes": 5120, "referer": "-",
            "user_agent": "Netflix/8.112.0 (Android)", "request_time": 0.2 + i}, _t(i),
            host="i-0eg1"))
    return out


def _span(tid, sid, parent, name, start, dur_ms, error=False):
    return {"traceId": tid, "spanId": sid, "parentSpanId": parent, "name": name, "kind": 2,
            "start": start, "end": start + timedelta(milliseconds=dur_ms),
            "attributes": {"rpc.system": "grpc"}, "status": {"code": 2 if error else 0}}


def _traces():
    by_app: dict[str, list] = {}
    for n in range(4):
        tid = f"{n + 1:032x}"
        s0 = _t(10 * n)
        err = n < 2
        spans = [
            ("lolomo-svc", _span(tid, f"{n:02x}00000000000001", "", "GetLolomo", s0, 900)),
            ("row-ranker", _span(tid, f"{n:02x}00000000000002", f"{n:02x}00000000000001",
                                 "RankRows", s0 + timedelta(milliseconds=5), 800)),
            ("title-batch-loader", _span(tid, f"{n:02x}00000000000003", f"{n:02x}00000000000002",
                                         "LoadTitles", s0 + timedelta(milliseconds=10), 700,
                                         error=err)),
            ("ratings-svc", _span(tid, f"{n:02x}00000000000004", f"{n:02x}00000000000001",
                                  "GetRatings", s0 + timedelta(milliseconds=6), 20)),
        ]
        for app, sp in spans:
            by_app.setdefault(app, []).append(sp)
    out = []
    for app, spans in by_app.items():
        tags = ("evidence:slow_trace",) if app == "lolomo-svc" else ()
        out.append(_r("traces", "traces/otlp/1452.jsonl", "otlp_jsonl", {
            "resource": {"service.name": app, "host.name": f"i-{app[:4]}"},
            "scope": "e2ekg", "spans": spans}, spans[0]["start"], host=f"i-{app[:4]}", tags=tags))
    out.append(_r("traces", "traces/zipkin/1452.jsonl", "zipkin_jsonl", {
        "traceId": f"{1:032x}", "id": "0000000000000005", "parentId": "0000000000000001",
        "name": "getartwork", "kind": "CLIENT", "timestamp": _t(0), "duration": 12000,
        "localEndpoint": {"serviceName": "artwork-metadata-svc"}, "tags": {"error": "true"}},
        _t(0)))
    return out


def _events():
    sp = "events/spinnaker.jsonl"
    out = [
        _r("events", sp, "spinnaker_jsonl", {
            "id": "pipe-001", "type": "deploy", "app": "api-gateway",
            "version": "api-gateway-v7.31.2", "strategy": "red-black", "status": "SUCCEEDED",
            "ts": _t(1560), "description": "Deploy api-gateway to prod"}, _t(1560)),
        _r("events", sp, "spinnaker_jsonl", {
            "id": "pipe-002", "type": "deploy", "app": "row-ranker",
            "version": "row-ranker-v3.2.0", "strategy": "red-black", "status": "SUCCEEDED",
            "ts": _t(60), "description": "Deploy row-ranker"}, _t(60)),
        _r("events", "events/properties.log", "properties_log", {
            "ts": T0, "app": "title-metadata-svc",
            "property": "title-metadata-svc.asg.minInstances", "old": "48", "new": "12",
            "user": "deploy-bot"}, T0, tags=("evidence:config_change",)),
    ]
    for i, (prev, des) in enumerate(((56, 48), (48, 40), (40, 32))):
        out.append(_r("events", "events/asg.jsonl", "asg_jsonl", {
            "ts": _t(480 + 120 * i), "app": "title-metadata-svc", "asg": "title-metadata-svc-v042",
            "previous_capacity": prev, "desired_capacity": des, "cause": "scale-in policy"},
            _t(480 + 120 * i), tags=("evidence:autoscaling",) if i == 0 else ()))
    out.append(_r("events", "events/ab_allocations.csv", "ab_csv", {"header": True}, None))
    out.append(_r("events", "events/ab_allocations.csv", "ab_csv", {
        "ts": _t(780), "experiment": "row-ranker-cell-b", "cell": "B", "old_allocation_pct": 0,
        "new_allocation_pct": 5, "population": "members, global", "app": "row-ranker",
        "actor": "j.mbeki"}, _t(780)))
    out.append(_r("events", "events/commits.log", "gitlog", {
        "sha": "a" * 40, "author": "Dana Lee <dana@example.com>", "date": _t(-3600),
        "message": "Lower minInstances for title-metadata-svc\n\nCost savings."}, _t(-3600)))
    out.append(_r("events", "alerts/radar.jsonl", "radar_jsonl", {
        "alert_id": "radar-9001", "ts": _t(2280), "app": "homepage-graphql",
        "severity": "critical", "metric": "client.ttr.p90", "state": "firing",
        "summary": "TTR p90 above threshold"}, _t(2280), tags=("evidence:alert",)))
    out.append(_r("events", "pagerduty/pages.jsonl", "pagerduty_jsonl", {
        "id": "PD-77", "ts": _t(2300), "app": "homepage-graphql", "urgency": "high",
        "title": "TTR regression", "status": "triggered"}, _t(2300)))
    out.append(_r("events", "incidents/incident_io.jsonl", "incident_jsonl", {
        "id": "INC-5377", "ts": _t(2520), "name": "Homepage TTR regression", "severity": "SEV2",
        "affected_services": ["api-gateway"], "status": "investigating"}, _t(2520),
        tags=("evidence:incident",)))
    return out


_MSGS = (
    "anyone else seeing homepage TTR spike?",
    "api-gateway deployed at 15:18, probably that",
    "title-metadata-svc latency is way up",
    "row-ranker pools look saturated",
    "lolomo-svc timeouts climbing",
    "checking homepage-graphql partial rows",
    "can we bump min to 40 on title-metadata-svc?",
    "edge-gateway is shedding load now",
    "Look at the dashboard for evcache-titles too",
    "nothing obvious in the client release",
)


def _chat():
    p = "slack/inc-5377-homepage-ttr/messages.jsonl"
    return [_r("chat", p, "slack_jsonl", {"type": "message", "user": f"U{i:04d}", "text": m,
                                          "ts": _t(2400 + 30 * i)}, _t(2400 + 30 * i))
            for i, m in enumerate(_MSGS)]


def build_hero_fixture_records() -> list[RawRecord]:
    return _catalog() + _atlas() + _prom() + _logs() + _traces() + _events() + _chat()


@pytest.fixture
def hero_fixture_records():
    """Returns the builder so tests read `hero_fixture_records()` like the brief."""
    return build_hero_fixture_records
