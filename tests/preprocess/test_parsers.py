"""Parsers: byte spans, field recovery, residuals, quarantine reasons, sniffing.

The generator's FORMATTERS render the inputs (tests may use the generator);
the parsers themselves never import it (checked below)."""
import ast
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from e2ekg.generate.formats import AB_COLUMNS, FORMATTERS, Stamp
from e2ekg.preprocess.parsers import PARSERS, Truncated, Unparseable, detect_format

T = datetime(2026, 10, 14, 21, 52, 3, 250000, tzinfo=UTC)

STACK_FIXTURE = (
    b"2026-10-14 21:52:03,250 ERROR [grpc-default-executor-3] com.netflix.artwork.Handler"
    b" - Unhandled exception rendering row\n"
    b"java.lang.NullPointerException: Cannot invoke \"String.length()\"\n"
    b"\tat com.netflix.artwork.Handler.render(Handler.java:88)\n"
    b"\tat com.netflix.artwork.Handler.handle(Handler.java:41)\n"
    b"Caused by: java.lang.IllegalStateException: no locale\n"
    b"\t... 12 more\n"
    b"2026-10-14 21:52:04,001 INFO [main] com.netflix.artwork.Health - Health check OK\n"
)


def spans(fmt: str, data: bytes) -> list[bytes]:
    return [data[o:o + n] for o, n in PARSERS[fmt].split(data)]


def test_log4j_stacktrace_is_one_row():
    got = list(PARSERS["log4j"].split(STACK_FIXTURE))
    assert len(got) == 2
    first, second = spans("log4j", STACK_FIXTURE)
    assert first.endswith(b"\t... 12 more") and first.count(b"\n") == 5
    assert second.startswith(b"2026-10-14 21:52:04,001")
    row = PARSERS["log4j"].parse(first)
    assert row["fields"]["stack"][0].startswith("java.lang.NullPointerException")
    assert row["ts_raw"] == "2026-10-14 21:52:03,250"
    assert row["cols"]["level"] == "ERROR"


def test_unknown_fields_go_to_residual():
    row = PARSERS["atlas"].parse(b'{"name":"x","tags":{},"t":1,"v":2,"zz":9}')
    assert row["_residual"] == {"zz": 9}
    assert row["fields"] == {"name": "x", "tags": {}, "t": 1, "v": 2}


def test_line_split_keeps_blank_lines_and_unterminated_tail():
    data = b'{"a":1}\n\n{"b":2}'
    assert list(PARSERS["json_lines"].split(data)) == [(0, 7), (8, 0), (9, 7)]


def test_truncated_and_latin1_json_are_distinguished():
    p = PARSERS["json_lines"]
    with pytest.raises(Truncated):
        p.parse(b'{"ts":"2026-10-14T19:00:00.000Z","msg":"Loaded 27')
    with pytest.raises(Truncated):  # cut inside a multi-byte character
        p.parse('{"ts":1,"msg":"Ångström"}'.encode()[:17])
    with pytest.raises(UnicodeDecodeError):
        p.parse('{"ts":1,"msg":"Ångström"}'.encode("latin-1"))
    with pytest.raises(Unparseable):
        p.parse(b'{"ts":1,"msg":"x"}}')


# generator fmt name -> pipeline format name, where they differ
_GEN = {"json_lines": "json", "otlp": "otlp_jsonl", "zipkin": "zipkin_jsonl",
        "properties": "properties_log", "slack": "slack_jsonl"}


def _round(fmt: str, fields: dict) -> dict:
    return PARSERS[fmt].parse(FORMATTERS[_GEN.get(fmt, fmt)](fields))


def test_json_log_fields_and_pii_residual():
    row = _round("json_lines", {"ts": Stamp(T, "epoch_ms"), "level": "INFO",
                                "app": "HOMEPAGE-GRAPHQL", "host": "i-01", "logger": "c.n.X",
                                "thread": "main", "msg": "hi", "request_id": "ab12",
                                "user_email": "a@b.com"})
    assert row["ts_raw"] == str(int(T.timestamp() * 1000))
    assert row["surface_app"] == "HOMEPAGE-GRAPHQL"
    assert row["cols"]["request_id"] == "ab12"
    assert row["_residual"] == {"user_email": "a@b.com"}


def test_logfmt_quoting_round_trips():
    row = _round("logfmt", {"ts": Stamp(T, "iso_pt"), "level": "info", "app": "row-ranker",
                            "caller": "RequestHandler", "msg": 'said "hi" = ok\tthen',
                            "request_id": "ff", "upstream": "x y"})
    assert row["fields"]["msg"] == 'said "hi" = ok\tthen'
    assert row["ts_raw"] == "2026-10-14T14:52:03.250-07:00"
    assert row["cols"]["level"] == "INFO" and row["cols"]["request_id"] == "ff"
    assert row["_residual"] == {"upstream": "x y"}


def test_log4j_request_id_in_message():
    row = _round("log4j", {"ts": Stamp(T, "log4j_pt"), "level": "INFO", "thread": "t-1",
                           "logger": "c.n.tbl.client.UpstreamClient",
                           "msg": "Calling title-metadata-svc.GetX request_id=00ff10"})
    assert row["ts_raw"] == "2026-10-14 14:52:03,250"
    assert row["cols"]["request_id"] == "00ff10"
    assert row["fields"]["stack"] == []


def test_nginx_line():
    row = _round("nginx", {"remote_addr": "10.0.0.1", "remote_user": "sofia@x.com",
                           "ts": Stamp(T, "nginx_pt"), "method": "POST",
                           "path": "/graphql?op=HomePage", "protocol": "HTTP/1.1", "status": 504,
                           "bytes": 0, "referer": "-", "user_agent": "Netflix/7.3.1 (TV)",
                           "request_time": 2.0})
    assert row["ts_raw"] == "14/Oct/2026:14:52:03 -0700"
    f = row["fields"]
    assert (f["status"], f["path"], f["remote_user"], f["request_time"]) == (
        "504", "/graphql?op=HomePage", "sofia@x.com", "2.000")


def test_prom_lines():
    help_ = _round("prom", {"kind": "help", "name": "requests_total", "text": "Total."})
    assert help_["cols"]["prom_kind"] == "help" and help_["ts_raw"] is None
    s = _round("prom", {"kind": "sample", "name": "requests_total",
                        "labels": {"service": 'a"b', "instance": "i-1"}, "value": float("nan"),
                        "t": T})
    assert s["fields"]["labels"] == {"service": 'a"b', "instance": "i-1"}
    assert s["cols"]["value_raw"] == "NaN" and s["surface_app"] == 'a"b'
    assert s["ts_raw"] == str(int(T.timestamp() * 1000))


def test_otlp_explodes_spans_and_zipkin_is_one_span():
    span = {"traceId": "a" * 32, "spanId": "b" * 16, "parentSpanId": "", "name": "GET /x",
            "kind": 2, "start": T, "end": T.replace(microsecond=500000),
            "attributes": {"http.status_code": 504}, "status": {"code": 2}}
    child = dict(span, spanId="c" * 16, parentSpanId="b" * 16, kind=3)
    row = _round("otlp", {"resource": {"service.name": "edge-gateway", "host.name": "i-9"},
                          "scope": "s", "spans": [span, child]})
    a, b = row["rows"]
    assert a["_residual"] == b["_residual"] == {}
    assert a["cols"]["host"] == "i-9" and a["surface_app"] == "edge-gateway"
    assert a["cols"]["span_kind"] == "SERVER" and a["cols"]["status_code"] == "ERROR"
    assert a["cols"]["parent_span_id"] is None and b["cols"]["parent_span_id"] == "b" * 16
    assert a["cols"]["duration_us"] == 250000
    odd = PARSERS["otlp"].parse(
        b'{"resource":{"attributes":[],"droppedAttributesCount":1},"schemaUrl":"u",'
        b'"scopeSpans":[{"scope":{"name":"s","version":"2"},"schemaUrl":"v","spans":['
        b'{"traceId":"a","spanId":"b","startTimeUnixNano":"1","endTimeUnixNano":"2",'
        b'"zz":9}]}]}')
    assert odd["rows"][0]["_residual"] == {
        "schemaUrl": "u", "resource.droppedAttributesCount": 1, "scopeSpans.schemaUrl": "v",
        "scope.version": "2", "zz": 9}
    z = _round("zipkin", {"traceId": "a" * 32, "id": "d" * 16, "parentId": "b" * 16,
                          "name": "get", "kind": "CLIENT", "timestamp": T, "duration": 12000,
                          "localEndpoint": {"serviceName": "ratings-svc"},
                          "tags": {"host.name": "i-7", "error": "true"}})
    assert z["cols"]["host"] == "i-7" and z["cols"]["status_code"] == "ERROR"
    assert z["ts_raw"] == str(int(T.timestamp() * 1_000_000))


def test_csv_header_gives_columns():
    header = FORMATTERS["ab_csv"]({"header": True})
    row = FORMATTERS["ab_csv"]({c: "members, global" if c == "population" else "v"
                                for c in AB_COLUMNS})
    data = header + b"\n" + row + b"\n"
    p = PARSERS["csv_events"].for_file(data)
    h, r = (p.parse(data[o:o + n]) for o, n in p.split(data))
    assert h["cols"]["row_kind"] == "header" and h["fields"]["columns"] == list(AB_COLUMNS)
    assert r["fields"]["population"] == "members, global" and r["cols"]["row_kind"] == "record"


def test_gitlog_split_and_parse():
    one = FORMATTERS["gitlog"]({"sha": "a" * 40, "author": "Dana <d@x>", "date": T,
                                "message": "[row-ranker] Lower min\n\ncommit msg body"})
    two = FORMATTERS["gitlog"]({"sha": "b" * 40, "author": "E <e@x>", "date": T, "message": "x"})
    data = one + b"\n" + two + b"\n"
    assert spans("gitlog", data) == [one, two]
    row = PARSERS["gitlog"].parse(one)
    assert row["surface_app"] == "row-ranker" and row["ts_raw"] == "Wed Oct 14 21:52:03 2026 +0000"
    assert row["fields"]["message"] == "[row-ranker] Lower min\n\ncommit msg body"


def test_catalog_documents_split_on_dashes():
    docs = [FORMATTERS["catalog_yaml"]({"id": a, "aliases": [a.upper()], "kind": "service"})
            for a in ("x-svc", "y-svc")]
    data = b"\n".join(docs) + b"\n"
    assert spans("yaml_catalog", data) == docs
    assert PARSERS["yaml_catalog"].parse(docs[1])["surface_app"] == "y-svc"


def test_properties_line():
    row = _round("properties", {"ts": T, "app": "title-metadata-svc",
                                "property": "title-metadata-svc.asg.minInstances", "old": 48,
                                "new": 12, "user": "v.rao", "id": "fpc-1"})
    assert row["surface_app"] == "title-metadata-svc" and row["fields"]["new"] == "12"
    assert row["cols"]["external_id"] == "fpc-1"


def test_slack_message():
    row = _round("slack", {"type": "message", "user": "U1", "text": "hi", "ts": T,
                           "thread_ts": T})
    assert row["ts_raw"] == "1792014723.250000" and row["cols"]["thread_ts"] == row["ts_raw"]


@pytest.mark.parametrize(("rel", "fmt", "line"), [
    ("logs/a/i-1.log", "json_lines", b'{"ts":1,"msg":"x"}'),
    ("logs/a/i-1.log", "logfmt", b"ts=2026-10-14T12:00:00.030-07:00 level=info msg=x"),
    ("logs/a/i-1.log", "log4j", b"2026-10-14 12:01:30,599 INFO [main] c.X - hi"),
    ("logs/a/i-1.log", "nginx",
     b'1.2.3.4 - - [14/Oct/2026:19:00:02 +0000] "GET / HTTP/1.1" 200 1 "-" "ua" rt=0.1'),
    ("metrics/atlas.jsonl", "atlas", b'{"name":"x","tags":{},"t":1,"v":2}'),
    ("metrics/prom/a-1200.prom", "prom", b"# HELP x y"),
    ("traces/otlp/1200.jsonl", "otlp", b'{"resource":{},"scopeSpans":[]}'),
    ("traces/zipkin/1200.jsonl", "zipkin", b'{"traceId":"1","id":"2"}'),
    ("events/spinnaker.jsonl", "spinnaker", b'{"id":"01J","pipeline":"p","ts":1}'),
    ("events/asg.jsonl", "asg", b'{"ts":"x","activity_id":"u","desired_capacity":1}'),
    ("events/properties.log", "properties",
     b"2026-10-14T20:25:00.000Z INFO  [archaius-poller] PropertyChanged app=x"),
    ("events/ab_allocations.csv", "csv_events", b"ts,experiment,cell"),
    ("events/commits.log", "gitlog", b"commit " + b"a" * 40),
    ("alerts/radar.jsonl", "radar", b'{"alert_id":"r","ts":"x"}'),
    ("pagerduty/pages.jsonl", "pagerduty", b'{"id":"P1","ts":"x","urgency":"high"}'),
    ("incidents/incident_io.jsonl", "incident", b'{"id":"INC-1","event":"incident.created"}'),
    ("slack/inc-1/messages.jsonl", "slack", b'{"type":"message","ts":"1.0"}'),
    ("catalog/apps.yaml", "yaml_catalog", b"---\nid: x"),
    ("logs/a/i-1.log", "unknown", b"\x00\x01\x02 binary junk"),
    ("mystery/file.bin", "unknown", b'{"a":1}'),
])
def test_detect_format(rel, fmt, line):
    assert detect_format(rel, line + b"\n" + line + b"\n") == fmt


def test_detect_format_survives_a_bad_first_line():
    head = "2026-10-14 12:01:30,599 INFO [main] c.X - Ångström\n".encode("latin-1")
    head += b"2026-10-14 12:01:31,000 INFO [main] c.X - ok\n" * 3
    assert detect_format("logs/a/i-1.log", head) == "log4j"


PKG = Path(__file__).resolve().parents[2] / "src" / "e2ekg" / "preprocess"


def test_preprocess_never_imports_generate_statically():
    bad = []
    for path in PKG.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            bad += [f"{path.name}: {n}" for n in names if n.startswith("e2ekg.generate")]
    assert bad == []


def test_preprocess_never_imports_generate_at_runtime():
    code = ("import sys, e2ekg.preprocess.run, e2ekg.preprocess.ledger, e2ekg.cli;"
            "print([m for m in sys.modules if m.startswith('e2ekg.generate')])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
