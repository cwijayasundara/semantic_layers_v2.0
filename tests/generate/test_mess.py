import json
from collections import Counter
from datetime import timedelta

from helpers import rng

from e2ekg.generate.mess import INJECTORS, MessInjection, apply_mess
from e2ekg.generate.records import RawRecord, write_source
from e2ekg.scenario.hero import hero_scenario

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


def test_sampling_records_decision_counts(hero_fixture_records):
    recs, inj = messed(hero_fixture_records)
    s = [json.loads(i.detail) for i in by_id(inj)["trc.sampling"]]
    assert s and all({"traces_seen", "kept_head", "kept_error", "dropped_traces",
                      "dropped_spans"} <= d.keys() for d in s)
    # The two error traces (ids 1 and 2) are always kept.
    kept = set()
    for r in recs:
        if r.fmt == "otlp_jsonl":
            d = json.loads(r.payload)
            kept |= {sp["traceId"] for ss in d["scopeSpans"] for sp in ss["spans"]}
    assert {f"{1:032x}", f"{2:032x}"} <= kept


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
