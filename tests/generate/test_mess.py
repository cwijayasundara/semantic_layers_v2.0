import json
from collections import Counter
from datetime import timedelta

from helpers import rng

from e2ekg.generate.formats import FORMATTERS
from e2ekg.generate.mess import INJECTORS, MessInjection, apply_mess
from e2ekg.generate.mess.logs import encoding, latin1_undecodable
from e2ekg.generate.mess.traces import (
    SAMPLING_IGNORANT_APPS,
    head_sampled,
    sampling,
    zipkin_legacy,
)
from e2ekg.generate.records import RawRecord, write_source
from e2ekg.scenario.hero import hero_scenario
from e2ekg.timeutil import pt

T = pt(2026, 10, 14, 14, 52, 3)

IDS = (
    "id.alias_surface", "id.casing", "id.suffix", "id.kind_confusion", "id.stale_catalog_alias",
    "id.slack_only_rename",
    "met.tag_key_drift", "met.unit_seconds", "met.counter_reset", "met.gap", "met.late",
    "met.duplicate_series",
    "log.tz_mix", "log.host_skew", "log.truncate", "log.duplicate", "log.interleave", "log.pii",
    "log.encoding",
    "trc.orphan", "trc.skew", "trc.zipkin_legacy", "trc.sampling",
    "evt.missing_field", "evt.duplicate_webhook",
    "chat.typo", "chat.nickname", "chat.edit", "chat.retraction",
)


def messed(builder, seed=7):
    return apply_mess(builder(), rng(seed), hero_scenario())


def by_id(inj):
    out: dict[str, list[MessInjection]] = {}
    for i in inj:
        out.setdefault(i.injector_id, []).append(i)
    return out


def test_registry_ids_unique_and_complete():
    ids = [i.id for i in INJECTORS]
    assert len(ids) == len(set(ids)) == 29


def test_registry_order_matches_brief():
    assert tuple(i.id for i in INJECTORS) == tuple(IDS)


def test_every_injector_fires_on_hero_fixture(hero_fixture_records):
    _, inj = apply_mess(hero_fixture_records(), rng(7), hero_scenario())
    assert {i.injector_id for i in inj} == {i.id for i in INJECTORS}


def test_tags_survive_mess(hero_fixture_records):
    recs, _ = apply_mess(hero_fixture_records(), rng(7), hero_scenario())
    assert any("evidence:pool_exhaustion" in r.tags for r in recs)


def test_details_are_json_with_locator(hero_fixture_records):
    _, inj = messed(hero_fixture_records)
    for i in inj:
        d = json.loads(i.detail)
        assert isinstance(d, dict) and d, i
        assert i.rel_path


def test_injections_respect_declared_sources(hero_fixture_records):
    src = {r.rel_path: r.source for r in hero_fixture_records()}
    recs, inj = messed(hero_fixture_records)
    src |= {r.rel_path: r.source for r in recs}
    sources = {i.id: i.sources for i in INJECTORS}
    for i in inj:
        assert src[i.rel_path] in sources[i.injector_id], i


def test_phases():
    phases = {i.id: i.phase for i in INJECTORS}
    assert set(phases.values()) == {"structured", "bytes"}
    assert phases["log.truncate"] == phases["log.encoding"] == "bytes"
    assert phases["log.tz_mix"] == phases["id.alias_surface"] == "structured"


def test_phase_ordering_structured_then_serialize_then_bytes():
    seen = []

    class Spy:
        sources = frozenset({"logs"})

        def __init__(self, id, phase):
            self.id, self.phase = id, phase

        def apply(self, recs, rng, scenario):
            seen.append((self.id, [r.payload is None for r in recs]))
            return recs, []

    r = RawRecord(source="logs", rel_path="a.log", fmt="json", fields={"msg": "x"})
    out, _ = apply_mess([r], rng(7), hero_scenario(),
                        injectors=(Spy("b1", "bytes"), Spy("s1", "structured"),
                                   Spy("b2", "bytes"), Spy("s2", "structured")))
    assert seen == [("s1", [True]), ("s2", [True]), ("b1", [False]), ("b2", [False])]
    assert out[0].payload == b'{"msg":"x"}'


def test_output_is_fully_serialized(hero_fixture_records):
    recs, _ = messed(hero_fixture_records)
    assert all(isinstance(r.payload, bytes) and r.payload for r in recs)


def test_same_seed_same_bytes(hero_fixture_records, tmp_path):
    for name in ("a", "b"):
        recs, inj = messed(hero_fixture_records)
        write_source(recs, tmp_path / name)
        (tmp_path / name / "_inj.json").write_text(json.dumps([i.__dict__ for i in inj]))
    files = sorted(p.relative_to(tmp_path / "a") for p in (tmp_path / "a").rglob("*")
                   if p.is_file())
    assert files
    for f in files:
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes(), f


def test_different_seed_differs(hero_fixture_records):
    a, _ = messed(hero_fixture_records, 7)
    b, _ = messed(hero_fixture_records, 8)
    assert [r.payload for r in a] != [r.payload for r in b]


def test_input_records_not_mutated(hero_fixture_records):
    before = hero_fixture_records()
    snap = [json.dumps(r.fields, default=str, sort_keys=True) for r in before]
    apply_mess(before, rng(7), hero_scenario())
    assert [json.dumps(r.fields, default=str, sort_keys=True) for r in before] == snap


def test_no_non_trace_record_is_dropped(hero_fixture_records):
    """Only trc.sampling may drop records (never-collected spans)."""
    before = Counter(r.rel_path for r in hero_fixture_records() if r.source != "traces")
    recs, _ = messed(hero_fixture_records)
    after = Counter(r.rel_path for r in recs if r.source != "traces")
    for path, n in before.items():
        assert after[path] >= n, path


def test_every_evidence_tag_survives_outside_traces(hero_fixture_records):
    before = Counter(t for r in hero_fixture_records() if r.source != "traces" for t in r.tags)
    recs, _ = messed(hero_fixture_records)
    after = Counter(t for r in recs if r.source != "traces" for t in r.tags)
    for t, n in before.items():
        assert after[t] >= n, t


def test_host_skew_shifts_one_tbl_host_by_90s(hero_fixture_records):
    _, inj = messed(hero_fixture_records)
    hs = by_id(inj)["log.host_skew"]
    assert len(hs) == 1 and hs[0].rel_path.startswith("logs/title-batch-loader/")
    assert json.loads(hs[0].detail)["skew_s"] == 90


def test_host_skew_changes_rendered_ts_not_at_utc(hero_fixture_records):
    from e2ekg.generate.mess._base import ts_utc
    recs, inj = messed(hero_fixture_records)
    path = by_id(inj)["log.host_skew"][0].rel_path
    shifted = [r for r in recs if r.rel_path == path]
    assert shifted and all(ts_utc(r.fields["ts"]) - r.at_utc == timedelta(seconds=90)
                           for r in shifted)


def test_encoding_one_latin1_line_per_file_with_non_ascii(hero_fixture_records):
    recs, inj = messed(hero_fixture_records)
    enc = by_id(inj)["log.encoding"]
    paths = [i.rel_path for i in enc]
    assert len(paths) == len(set(paths)) >= 1
    for p in paths:
        bad = []
        for r in recs:
            if r.rel_path == p:
                try:
                    r.payload.decode("utf-8")
                except UnicodeDecodeError:
                    bad.append(r)
        assert len(bad) == 1
        assert "é".encode("latin-1") in bad[0].payload


def test_truncate_cuts_json_mid_object(hero_fixture_records):
    recs, inj = messed(hero_fixture_records)
    tr = by_id(inj)["log.truncate"]
    assert len(tr) >= 1
    for i in tr:
        d = json.loads(i.detail)
        cut = [r for r in recs if r.rel_path == i.rel_path][d["idx"]]
        assert len(cut.payload) == d["cut_at"] < d["original_len"]
        assert cut.payload.startswith(b"{") and not cut.payload.endswith(b"}")
        assert bytes.fromhex(d["removed_hex"])[-1:] == b"}"


def test_head_sampled_is_error_aware_and_about_one_percent():
    assert head_sampled("anything", has_error=True)
    ids = [f"{n:032x}" for n in range(20_000)]
    rate = sum(head_sampled(t) for t in ids) / len(ids)
    assert 0.007 < rate < 0.013
    assert [head_sampled(t) for t in ids[:500]] == [head_sampled(t) for t in ids[:500]]


def test_sampling_drops_only_ignorant_apps_per_trace(hero_fixture_records):
    before = hero_fixture_records()
    recs, inj = messed(hero_fixture_records)
    drops = [json.loads(i.detail) for i in by_id(inj)["trc.sampling"]]
    assert drops and all(d["trace_id"] and d["dropped_span_ids"] for d in drops)
    assert all(set(d["apps"]) <= SAMPLING_IGNORANT_APPS for d in drops)
    # Every non-legacy OTLP span survives.
    others = {sp["spanId"] for r in before if r.fmt == "otlp_jsonl"
              and r.fields["resource"]["service.name"] not in SAMPLING_IGNORANT_APPS
              for sp in r.fields["spans"]}
    after = {sp["spanId"] for r in recs if r.fmt == "otlp_jsonl"
             for ss in json.loads(r.payload)["scopeSpans"] for sp in ss["spans"]}
    assert others <= after


def _trace_rec(tid, sid, app, tags=(), rel="traces/zipkin/1500.jsonl"):
    return RawRecord(source="traces", rel_path=rel, fmt="zipkin_jsonl", tags=frozenset(tags),
                     fields={"traceId": tid, "id": sid, "name": "op", "timestamp": T,
                             "duration": 5, "localEndpoint": {"serviceName": app}, "tags": {}},
                     at_utc=T)


def test_sampling_never_drops_evidence_spans():
    tagged = [_trace_rec(f"{n:032x}", f"{n:016x}", "ratings-svc", ("evidence:slow_trace",))
              for n in range(40)]
    plain = [_trace_rec(f"{n:032x}", f"{n + 100:016x}", "ratings-svc") for n in range(40)]
    out, inj = sampling.apply(tagged + plain, rng(7), hero_scenario())
    assert all(r in out for r in tagged)
    dropped = {s for i in inj for s in json.loads(i.detail)["dropped_span_ids"]}
    assert dropped and dropped <= {r.fields["id"] for r in plain}
    assert len(out) == 80 - len(dropped)


def test_zipkin_conversion_is_lossless_for_a_rich_span():
    span = {"traceId": "ab" * 16, "spanId": "cd" * 8, "parentSpanId": "ef" * 8,
            "name": "GetRatings", "kind": 1, "start": T, "end": T + timedelta(milliseconds=7),
            "attributes": {"rpc.system": "grpc", "retries": 2, "ratio": 0.5, "cached": False,
                           "ids": [1, 2], "host.name": "span-host"},
            "status": {"code": 2, "message": "DEADLINE_EXCEEDED"},
            "events": [{"time": T, "name": "retry", "attributes": {"attempt": 1}}],
            "traceState": "vendor=1"}
    rec = RawRecord(source="traces", rel_path="traces/otlp/1500.jsonl", fmt="otlp_jsonl",
                    fields={"resource": {"service.name": "RatingsSvc", "host.name": "res-host",
                                         "region": "us-west-2"},
                            "scope": "netflix.tracer", "spans": [span]},
                    at_utc=T, tags=frozenset({"evidence:slow_trace"}))
    out, inj = zipkin_legacy.apply([rec], rng(7), hero_scenario())
    (z,) = out
    assert z.tags == rec.tags and z.rel_path == "traces/zipkin/1500.jsonl"
    d = json.loads(FORMATTERS["zipkin_jsonl"](z.fields))
    assert d["traceId"] == span["traceId"] and d["id"] == span["spanId"]
    assert d["parentId"] == span["parentSpanId"]
    assert d["name"] == "GetRatings"
    assert d["localEndpoint"] == {"serviceName": "RatingsSvc"}
    assert d["timestamp"] == 1792014723000000 and d["duration"] == 7000
    assert "kind" not in d and d["tags"]["otel.span_kind"] == "1"
    t = d["tags"]
    assert t["rpc.system"] == "grpc" and t["retries"] == "2" and t["ratio"] == "0.5"
    assert t["cached"] == "false" and json.loads(t["ids"]) == [1, 2]
    assert t["host.name"] == "span-host" and t["otel.resource.host.name"] == "res-host"
    assert t["region"] == "us-west-2"
    assert t["otel.scope.name"] == "netflix.tracer"
    assert t["otel.status_code"] == "ERROR" and t["error"] == "DEADLINE_EXCEEDED"
    assert t["otel.traceState"] == "vendor=1"
    assert d["annotations"] == [{"timestamp": 1792014723000000, "value": 'retry {"attempt":1}'}]
    assert json.loads(inj[0].detail)["prefixed_resource_keys"] == ["host.name"]


def test_encoding_output_never_decodes_as_utf8():
    assert latin1_undecodable("Ã©".encode()) is None  # latin-1 bytes C3 A9 are valid UTF-8
    assert latin1_undecodable("é".encode()) == b"\xe9"
    assert latin1_undecodable(b"ascii") is None
    lines = [RawRecord(source="logs", rel_path="logs/a/h.log", fmt="logfmt", fields={},
                       payload=f"msg={m}".encode(), at_utc=T) for m in ("Ã©", "Ã©", "café")]
    lone = RawRecord(source="logs", rel_path="logs/b/h.log", fmt="logfmt", fields={},
                     payload="msg=Ã©".encode(), at_utc=T)
    for seed in range(10):
        out, inj = encoding.apply([*lines, lone], rng(seed), hero_scenario())
        assert [i.rel_path for i in inj] == ["logs/a/h.log"]
        assert out[2].payload == "msg=café".encode("latin-1")
        try:
            out[2].payload.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            raise AssertionError("still decodable")


def test_zipkin_legacy_moves_legacy_app_spans(hero_fixture_records):
    recs, _ = messed(hero_fixture_records)
    for r in recs:
        if r.fmt == "otlp_jsonl":
            names = json.loads(r.payload)["resource"]["attributes"]
            assert all(not (a["key"] == "service.name" and "ratings" in
                            a["value"]["stringValue"].lower()) for a in names)
    assert any(r.fmt == "zipkin_jsonl" and b"ratings" in r.payload.lower() for r in recs)


def test_slack_only_rename_only_in_chat(hero_fixture_records):
    recs, _ = messed(hero_fixture_records)
    assert any(r.source == "chat" and b"TMS" in r.payload for r in recs)
    assert not any(r.source != "chat" and b"TMS" in r.payload for r in recs)


def test_duplicates_copy_tags(hero_fixture_records):
    recs, inj = messed(hero_fixture_records)
    dups = by_id(inj)["log.duplicate"]
    assert dups
    for i in dups:
        d = json.loads(i.detail)
        same = [r for r in recs if r.rel_path == i.rel_path and r.at_utc.isoformat() == d["at_utc"]]
        assert len(same) >= 2 and len({r.tags for r in same}) == 1


def test_edit_and_retraction_add_messages(hero_fixture_records):
    n_before = sum(r.source == "chat" for r in hero_fixture_records())
    recs, inj = messed(hero_fixture_records)
    n_after = sum(r.source == "chat" for r in recs)
    ids = by_id(inj)
    assert n_after == n_before + len(ids["chat.edit"]) + len(ids["chat.retraction"])
    assert any(b"message_changed" in r.payload and b"previous_message" in r.payload for r in recs)
