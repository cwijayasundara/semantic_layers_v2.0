import hashlib

import pytest

from e2ekg.generate.records import RawRecord, serialize, write_source
from e2ekg.rawid import RawSpan, raw_id
from e2ekg.timeutil import pt


def rec(source, rel_path, payload, tags=()):
    return RawRecord(source=source, rel_path=rel_path, fmt="json", fields={}, payload=payload,
                     tags=frozenset(tags))


def test_raw_id_matches_bytes(tmp_path):
    out = write_source([rec("logs", "a.log", b"hello")], tmp_path)
    span = RawSpan.parse(out[0][0])
    assert (tmp_path / "a.log").read_bytes()[span.offset:span.offset + span.length] == b"hello"


def test_raw_id_format():
    h = hashlib.sha256(b"abc").hexdigest()[:16]
    assert raw_id("logs", "logs/a/b.log", 12, b"abc") == f"logs|logs/a/b.log|12|3|{h}"


def test_raw_span_parse_roundtrip():
    rid = raw_id("traces", "traces/otlp/1452.jsonl", 40, b"{}")
    s = RawSpan.parse(rid)
    assert s == RawSpan(raw_id=rid, source="traces", rel_path="traces/otlp/1452.jsonl",
                        offset=40, length=2)


@pytest.mark.parametrize("bad", ["", "a|b|c", "a|b|x|2|0123456789abcdef", "a|b|1|2|short"])
def test_raw_span_parse_rejects_garbage(bad):
    with pytest.raises(ValueError):
        RawSpan.parse(bad)


def test_write_source_offsets_per_path_and_tags(tmp_path):
    recs = [rec("logs", "x/a.log", b"one", ("t1",)), rec("logs", "x/b.log", b"zz"),
            rec("logs", "x/a.log", b"three", ("t2",))]
    out = write_source(recs, tmp_path)
    assert (tmp_path / "x/a.log").read_bytes() == b"one\nthree\n"
    assert (tmp_path / "x/b.log").read_bytes() == b"zz\n"
    spans = [RawSpan.parse(r) for r, _ in out]
    assert [(s.rel_path, s.offset, s.length) for s in spans] == [
        ("x/a.log", 0, 3), ("x/b.log", 0, 2), ("x/a.log", 4, 5)]
    assert [t for _, t in out] == [frozenset({"t1"}), frozenset(), frozenset({"t2"})]


def test_write_source_refuses_existing_file(tmp_path):
    write_source([rec("logs", "a.log", b"first")], tmp_path)
    with pytest.raises(FileExistsError):
        write_source([rec("logs", "b.log", b"x"), rec("logs", "a.log", b"second")], tmp_path)
    assert (tmp_path / "a.log").read_bytes() == b"first\n"
    assert not (tmp_path / "b.log").exists()


def test_write_source_serializes_records_without_payload(tmp_path):
    r = RawRecord(source="atlas", rel_path="m.jsonl", fmt="atlas_jsonl",
                  fields={"name": "x", "tags": {"nf.app": "a"}, "t": pt(2026, 10, 14, 12, 0),
                          "v": 1.0})
    write_source([r], tmp_path)
    assert (tmp_path / "m.jsonl").read_bytes() == (
        b'{"name":"x","tags":{"nf.app":"a"},"t":1792004400000,"v":1.0}\n')


def test_serialize_fills_payload_once():
    r = RawRecord(source="logs", rel_path="a.log", fmt="json", fields={"msg": "hi"})
    s = serialize(r)
    assert s.payload == b'{"msg":"hi"}' and r.payload is None
    assert serialize(s) is s


def test_tags_are_never_serialized(tmp_path):
    r = RawRecord(source="logs", rel_path="a.log", fmt="json", fields={"msg": "hi"},
                  tags=frozenset({"evidence:log_error"}))
    write_source([r], tmp_path)
    assert b"evidence" not in (tmp_path / "a.log").read_bytes()
