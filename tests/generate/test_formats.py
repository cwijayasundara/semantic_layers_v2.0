import json

import pytest
import yaml

from e2ekg.generate.formats import (
    FORMATTERS,
    JSON_FMTS,
    MULTILINE_FMTS,
    Stamp,
    render_ts,
)
from e2ekg.timeutil import pt

ALL_FMTS = {"json", "logfmt", "log4j", "nginx", "atlas_jsonl", "prom", "otlp_jsonl",
            "zipkin_jsonl", "spinnaker_jsonl", "properties_log", "asg_jsonl", "ab_csv", "gitlog",
            "radar_jsonl", "pagerduty_jsonl", "incident_jsonl", "slack_jsonl", "catalog_yaml"}
T = pt(2026, 10, 14, 14, 52, 3)  # 21:52:03 UTC


def test_formatters_cover_every_fmt():
    assert ALL_FMTS <= set(FORMATTERS)
    assert MULTILINE_FMTS == {"log4j", "gitlog", "catalog_yaml"}


def test_fixture_covers_every_fmt_and_source(hero_fixture_records):
    recs = hero_fixture_records()
    assert {r.fmt for r in recs} == ALL_FMTS
    assert {r.source for r in recs} == {"catalog", "atlas", "prom", "logs", "traces", "events",
                                        "chat"}


def test_round_trip_smoke_every_fixture_record(hero_fixture_records):
    for r in hero_fixture_records():
        out = FORMATTERS[r.fmt](r.fields)
        assert isinstance(out, bytes) and out, r.fmt
        assert not out.endswith(b"\n"), r.fmt
        if r.fmt not in MULTILINE_FMTS:
            assert b"\n" not in out and b"\r" not in out, (r.fmt, out)
        if r.fmt in JSON_FMTS:
            assert isinstance(json.loads(out), dict), r.fmt


def test_render_ts_styles():
    assert render_ts(T, "iso_z") == "2026-10-14T21:52:03.000Z"
    assert render_ts(T, "iso_pt") == "2026-10-14T14:52:03.000-07:00"
    assert render_ts(T, "epoch_ms") == 1792014723000
    assert render_ts(T, "log4j_pt") == "2026-10-14 14:52:03,000"
    assert render_ts(T, "nginx_utc") == "14/Oct/2026:21:52:03 +0000"
    assert render_ts(T, "git_utc") == "Wed Oct 14 21:52:03 2026 +0000"
    assert render_ts(T, "slack_ts") == "1792014723.000000"
    assert render_ts(T, "epoch_ns_str") == "1792014723000000000"


def test_stamp_overrides_fmt_default():
    a = FORMATTERS["json"]({"ts": T, "msg": "x"})
    b = FORMATTERS["json"]({"ts": Stamp(T, "epoch_ms"), "msg": "x"})
    c = FORMATTERS["json"]({"ts": "verbatim", "msg": "x"})
    assert json.loads(a)["ts"] == "2026-10-14T21:52:03.000Z"
    assert json.loads(b)["ts"] == 1792014723000
    assert json.loads(c)["ts"] == "verbatim"


def test_logfmt_quotes_and_escapes():
    out = FORMATTERS["logfmt"]({"ts": T, "level": "info", "msg": 'a "b"\nc', "n": 3})
    assert out == b'ts=2026-10-14T21:52:03.000Z level=info msg="a \\"b\\"\\nc" n=3'


def test_log4j_header_and_stack():
    out = FORMATTERS["log4j"]({"ts": T, "level": "ERROR", "thread": "t1", "logger": "c.X",
                               "msg": "boom", "stack": ["E: x", "\tat a.b(C.java:1)"]})
    assert out.split(b"\n") == [b"2026-10-14 21:52:03,000 ERROR [t1] c.X - boom", b"E: x",
                                b"\tat a.b(C.java:1)"]


def test_nginx_combined():
    out = FORMATTERS["nginx"]({"remote_addr": "10.0.0.1", "remote_user": "-", "ts": T,
                               "method": "GET", "path": "/x", "protocol": "HTTP/1.1",
                               "status": 504, "bytes": 10, "referer": "-", "user_agent": "UA"})
    assert out == (b'10.0.0.1 - - [14/Oct/2026:21:52:03 +0000] "GET /x HTTP/1.1" 504 10 '
                   b'"-" "UA"')


def test_atlas_shape():
    out = json.loads(FORMATTERS["atlas_jsonl"]({"name": "n", "tags": {"nf.app": "a"}, "t": T,
                                                "v": None}))
    assert out == {"name": "n", "tags": {"nf.app": "a"}, "t": 1792014723000, "v": None}


def test_prom_lines():
    f = FORMATTERS["prom"]
    assert f({"kind": "help", "name": "x_total", "text": "X."}) == b"# HELP x_total X."
    assert f({"kind": "type", "name": "x_total", "type": "counter"}) == b"# TYPE x_total counter"
    assert f({"kind": "sample", "name": "x_total", "labels": {"app": 'a"b', "i": "1"},
              "value": 3, "t": T}) == b'x_total{app="a\\"b",i="1"} 3 1792014723000'
    assert f({"kind": "sample", "name": "g", "labels": {}, "value": float("nan")}) == b"g NaN"


def test_otlp_resource_spans_shape():
    out = json.loads(FORMATTERS["otlp_jsonl"]({
        "resource": {"service.name": "a"}, "scope": "s",
        "spans": [{"traceId": "t", "spanId": "s1", "parentSpanId": "", "name": "op", "kind": 2,
                   "start": T, "end": T, "attributes": {"n": 1, "ok": True, "f": 0.5, "s": "x"},
                   "status": {"code": 2}}]}))
    assert out["resource"]["attributes"] == [{"key": "service.name", "value": {"stringValue": "a"}}]
    span = out["scopeSpans"][0]["spans"][0]
    assert out["scopeSpans"][0]["scope"] == {"name": "s"}
    assert span["startTimeUnixNano"] == "1792014723000000000"
    assert span["attributes"] == [
        {"key": "n", "value": {"intValue": "1"}}, {"key": "ok", "value": {"boolValue": True}},
        {"key": "f", "value": {"doubleValue": 0.5}}, {"key": "s", "value": {"stringValue": "x"}}]
    assert span["status"] == {"code": 2}


def test_otlp_events_rendered_as_otlp_events():
    out = json.loads(FORMATTERS["otlp_jsonl"]({
        "resource": {"service.name": "a"}, "scope": "s",
        "spans": [{"traceId": "t", "spanId": "s1", "name": "op", "kind": 2, "start": T, "end": T,
                   "events": [{"time": T, "name": "retry", "attributes": {"n": 2}}]}]}))
    assert out["scopeSpans"][0]["spans"][0]["events"] == [
        {"timeUnixNano": "1792014723000000000", "name": "retry",
         "attributes": [{"key": "n", "value": {"intValue": "2"}}]}]


def test_zipkin_timestamp_micros():
    out = json.loads(FORMATTERS["zipkin_jsonl"]({"traceId": "t", "id": "i", "timestamp": T,
                                                 "duration": 5}))
    assert out["timestamp"] == 1792014723000000


def test_ab_csv_header_and_quoted_row():
    f = FORMATTERS["ab_csv"]
    assert f({"header": True}).startswith(b"ts,experiment,cell,")
    row = f({"ts": T, "experiment": "e", "cell": "B", "population": "members, global"})
    assert b'"members, global"' in row and row.startswith(b"2026-10-14T21:52:03.000Z,e,B,")


def test_gitlog_block():
    out = FORMATTERS["gitlog"]({"sha": "a" * 40, "author": "D <d@x>", "date": T,
                                "message": "Subject\n\nBody"})
    assert out.decode().split("\n") == [
        "commit " + "a" * 40, "Author: D <d@x>", "Date:   Wed Oct 14 21:52:03 2026 +0000", "",
        "    Subject", "    ", "    Body"]


def test_gitlog_message_bytes_stay_faithful():
    """No splitlines(): \r\n, \x0b, \x1c and a trailing newline survive."""
    msg = "a\r\nb\x0bc\x1cd\n"
    out = FORMATTERS["gitlog"]({"sha": "b" * 40, "author": "D <d@x>", "date": T, "message": msg})
    assert not out.endswith(b"\n") and b"\r" not in out
    body = out.decode().split("\n")[4:]
    assert body == ["    a\\r", "    b\x0bc\x1cd", "    "]
    assert "\n".join(ln[4:] for ln in body).replace("\\r", "\r") == msg


def test_catalog_yaml_document():
    out = FORMATTERS["catalog_yaml"]({"id": "a", "aliases": ["A"], "tier": 0})
    assert out.startswith(b"---\n")
    assert yaml.safe_load(out) == {"id": "a", "aliases": ["A"], "tier": 0}


def test_slack_ts_string():
    out = json.loads(FORMATTERS["slack_jsonl"]({"type": "message", "text": "hi", "ts": T}))
    assert out["ts"] == "1792014723.000000"


@pytest.mark.parametrize("fmt", ["json", "logfmt", "properties_log", "slack_jsonl"])
def test_non_ascii_is_utf8_not_escaped(fmt):
    assert "Amélie".encode() in FORMATTERS[fmt]({"ts": T, "msg": "Amélie", "text": "Amélie",
                                                 "app": "a", "property": "p"})
