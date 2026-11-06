"""Duplicate keys and lossy conversions: every value of a crafted record must
survive in `{**fields, **_residual}` (first value in place, later ones `key#n`)."""
import json

from e2ekg.preprocess.parsers import PARSERS


def merged(r: dict) -> dict:
    return {**r["fields"], **r["_residual"]}


def test_json_duplicate_keys_kept():
    r = PARSERS["json_lines"].parse(b'{"ts":1,"msg":"a","msg":"b","msg":"c","x":1,"x":2}')
    assert r["fields"]["msg"] == "a"
    m = merged(r)
    assert (m["msg#2"], m["msg#3"], m["x"], m["x#2"]) == ("b", "c", 1, 2)


def test_json_duplicate_key_nested_and_zipkin():
    r = PARSERS["zipkin"].parse(b'{"traceId":"t","id":"i","tags":{"k":"1","k":"2"},"id":"j"}')
    assert r["fields"]["tags"] == {"k": "1", "k#2": "2"}
    assert merged(r)["id#2"] == "j"


def test_logfmt_duplicate_keys():
    r = PARSERS["logfmt"].parse(b'ts=1 msg=a msg="b c" foo=1 foo=2')
    m = merged(r)
    assert (r["fields"]["msg"], m["msg#2"], m["foo"], m["foo#2"]) == ("a", "b c", "1", "2")


def test_prom_duplicate_labels():
    r = PARSERS["prom"].parse(b'm{a="1",a="2",b="x",a="3"} 5 100')
    assert r["fields"]["labels"] == {"a": "1", "b": "x"}
    assert merged(r)["a#2"] == "2" and merged(r)["a#3"] == "3"


def test_csv_duplicate_header_columns():
    data = b"ts,app,app,x\n1,a,b,c\n"
    p = PARSERS["csv_events"].for_file(data)
    _, rec = (p.parse(data[o:o + n]) for o, n in p.split(data))
    assert rec["fields"]["app"] == "a" and rec["_residual"] == {"app#2": "b"}
    assert merged(rec)["x"] == "c"


GIT = (b"commit " + b"a" * 40 + b"\nAuthor:  Dana <d@x>\nDATE:   Wed Oct 14 21:52:03 2026 +0000\n"
       b"Author: Eve <e@x>\nSigned-Off-By: z\nsigned-off-by: y\n\n    [svc] msg")


def test_gitlog_verbatim_headers_and_duplicates():
    r = PARSERS["gitlog"].parse(GIT)
    f = r["fields"]
    assert f["Author"] == "  Dana <d@x>"  # key casing and padding verbatim
    assert f["DATE"] == "   Wed Oct 14 21:52:03 2026 +0000"
    assert r["ts_raw"] == "Wed Oct 14 21:52:03 2026 +0000"
    m = merged(r)
    assert m["Author#2"] == " Eve <e@x>"
    assert m["Signed-Off-By"] == " z" and m["signed-off-by"] == " y"


def _otlp(obj: dict) -> dict:
    return PARSERS["otlp"].parse(json.dumps(obj).encode())


def _span(**kw) -> dict:
    return {"traceId": "a", "spanId": "b", "startTimeUnixNano": "1", "endTimeUnixNano": "2", **kw}


def test_otlp_non_dict_keyvalues_go_to_residual():
    r = _otlp({"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "s"}},
                                           "junk"]},
               "scopeSpans": [{"spans": [_span(attributes=[7, {"key": "k",
                                                               "value": {"stringValue": "v"}}])]}]})
    sp = r["rows"][0]
    assert sp["_residual"]["attributes[0]"] == 7
    assert sp["_residual"]["resource.attributes[1]"] == "junk"
    assert sp["cols"]["attributes"] == {"k": "v"}


def test_otlp_anyvalue_types_stay_distinguishable():
    attrs_ = [{"key": "s", "value": {"stringValue": "AAEC"}},
              {"key": "b", "value": {"bytesValue": "AAEC"}},
              {"key": "i", "value": {"intValue": "007"}},
              {"key": "n", "value": {"intValue": "7"}},
              {"key": "k", "value": {"kvlistValue": {"values": [{"key": "z"}, 5]}}},
              {"key": "s", "value": {"stringValue": "dup"}}]
    r = _otlp({"resource": {"attributes": attrs_}, "scopeSpans": [{"spans": [_span()]}]})
    res = r["rows"][0]["fields"]["resource"]
    assert res["s"] == "AAEC" and res["b"] == {"bytesValue": "AAEC"}
    assert res["i"] == {"intValue": "007"} and res["n"] == 7
    assert res["k"] == {"kvlistValue": {"values": [{"key": "z"}, 5]}}
    assert res["s#2"] == "dup"


def test_otlp_envelope_keys_are_prefixed_and_not_overwritten():
    r = _otlp({"resource": {"attributes": []}, "schemaUrl": "env",
               "scopeSpans": [{"spans": [_span(schemaUrl="span")]}]})
    res = r["rows"][0]["_residual"]
    assert res["resourceSpans.schemaUrl"] == "env" and res["schemaUrl"] == "span"
    assert r["_residual"]["resourceSpans.schemaUrl"] == "env"
