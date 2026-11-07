"""The coverage ledger: every raw byte in exactly one parsed or quarantine row."""
import json

import polars as pl
import pytest
from pp_helpers import injections, manifest, quarantine, table

from e2ekg.cli import build_parser
from e2ekg.preprocess.ledger import check_coverage
from e2ekg.preprocess.run import TABLES, preprocess_scenario
from e2ekg.rawid import RawSpan

# ---- synthetic trees: the ledger must catch what it claims to catch ----


def _tree(tmp_path, files: dict[str, bytes]):
    raw = tmp_path / "raw"
    for rel, data in files.items():
        (raw / rel).parent.mkdir(parents=True, exist_ok=True)
        (raw / rel).write_bytes(data)
    return raw


LOG = (b'{"ts":"2026-10-14T19:00:00.000Z","level":"INFO","msg":"a"}\n'
       b'{"ts":"2026-10-14T19:00:01.000Z","level":"INFO","msg":"b"}\n'
       + "{\"ts\":\"2026-10-14T19:00:02.000Z\",\"msg\":\"Ångström\"}\n".encode("latin-1")
       + b'{"ts":"2026-10-14T19:00:03.000Z","msg":"cut')


def test_small_tree_is_fully_covered(tmp_path):
    raw = _tree(tmp_path, {"logs/a/i-1.log": LOG, "mystery/x.bin": b"\x00\x01",
                           "events/empty.jsonl": b""})
    report = preprocess_scenario(raw, tmp_path / "proc", llm=None)
    r = check_coverage(raw, tmp_path / "proc")
    assert (r.files, r.gaps, r.overlaps, r.bytes_covered) == (3, [], [], r.bytes_total)
    assert r.bytes_total == len(LOG) + 2
    q = pl.read_parquet(tmp_path / "proc" / "quarantine.parquet")
    assert sorted(q["reason"].to_list()) == ["decode_error", "truncated", "unknown_format"]
    assert report.tables["logs"] == 2 and report.coverage["gaps"] == 0


def test_ledger_reports_gaps_and_overlaps(tmp_path):
    raw = _tree(tmp_path, {"logs/a/i-1.log": LOG})
    proc = tmp_path / "proc"
    preprocess_scenario(raw, proc, llm=None)
    logs = pl.read_parquet(proc / "logs.parquet")
    logs.slice(1).write_parquet(proc / "logs.parquet")
    r = check_coverage(raw, proc)
    assert [g.offset for g in r.gaps] == [0] and r.bytes_covered < r.bytes_total
    pl.concat([logs, logs.slice(1, 1)]).write_parquet(proc / "logs.parquet")  # same id twice
    q = pl.read_parquet(proc / "quarantine.parquet")
    extra = pl.DataFrame({"raw_id": [logs["raw_id"][1]], "source": ["logs"],
                          "rel_path": ["logs/a/i-1.log"], "reason": ["unparseable"],
                          "excerpt": ["x"]})
    pl.concat([q, extra], how="diagonal_relaxed").write_parquet(proc / "quarantine.parquet")
    r = check_coverage(raw, proc)
    assert r.gaps == [] and [o.raw_id for o in r.overlaps] == [logs["raw_id"][1]]


def test_ledger_rejects_a_forged_digest(tmp_path):
    raw = _tree(tmp_path, {"logs/a/i-1.log": LOG})
    proc = tmp_path / "proc"
    preprocess_scenario(raw, proc, llm=None)
    logs = pl.read_parquet(proc / "logs.parquet")
    s = RawSpan.parse(logs["raw_id"][0])
    forged = f"{s.source}|{s.rel_path}|{s.offset}|{s.length}|{'0' * 16}"
    logs.with_columns(pl.when(pl.int_range(pl.len()) == 0).then(pl.lit(forged))
                      .otherwise(pl.col("raw_id")).alias("raw_id")).write_parquet(
        proc / "logs.parquet")
    r = check_coverage(raw, proc)
    assert [m.raw_id for m in r.mismatches] == [forged]


# ---- the scale-0.05 hero fixture ----


def test_coverage_is_total(preprocessed_hero):
    r = check_coverage(preprocessed_hero.raw, preprocessed_hero.proc)
    assert r.gaps == [] and r.overlaps == [] and r.mismatches == []
    assert r.bytes_covered == r.bytes_total and r.files == len(manifest(preprocessed_hero)["files"])


def test_every_table_written_with_common_columns(preprocessed_hero):
    common = ["raw_id", "source", "rel_path", "ts_raw", "ts_utc", "skew_ms", "surface_app",
              "fields", "_residual"]
    for name in TABLES:
        df = table(preprocessed_hero, name)
        assert df.columns[:len(common)] == common, name
        assert df.height > 0, name


def test_latin1_line_quarantined_not_dropped(preprocessed_hero):
    q = quarantine(preprocessed_hero)
    n = len(injections(preprocessed_hero, "log.encoding"))
    assert (q["reason"] == "decode_error").sum() == n >= 1


def test_truncated_json_quarantined(preprocessed_hero):
    q = quarantine(preprocessed_hero)
    n = len(injections(preprocessed_hero, "log.truncate"))
    assert (q["reason"] == "truncated").sum() == n >= 1
    assert set(q["reason"]) <= {"decode_error", "truncated"}


def test_original_timestamp_kept(preprocessed_hero):
    df = table(preprocessed_hero, "logs")
    assert df["ts_raw"].null_count() == 0 and df["ts_utc"].null_count() == 0


def test_record_counts_match_what_was_written(preprocessed_hero):
    counts = manifest(preprocessed_hero)["counts"]
    q = quarantine(preprocessed_hero)
    assert table(preprocessed_hero, "logs").height + q.filter(
        pl.col("source") == "logs").height == counts["logs"]
    spans = table(preprocessed_hero, "spans")
    assert spans.height == counts["spans"] and spans["raw_id"].n_unique() == counts["traces"]
    assert table(preprocessed_hero, "metrics_atlas").height == counts["atlas"]
    assert table(preprocessed_hero, "metrics_prom").height == counts["prom"]
    assert table(preprocessed_hero, "slack").height == counts["chat"]
    assert table(preprocessed_hero, "catalog").height == counts["catalog"]
    events = sum(table(preprocessed_hero, t).height
                 for t in ("events", "alerts", "pages", "incidents"))
    assert events == counts["events"]


def test_duplicates_stay_separate_rows(preprocessed_hero):
    df = table(preprocessed_hero, "logs").with_columns(
        pl.col("raw_id").str.extract(r"\|([0-9a-f]{16})$").alias("digest"))
    repeated = df.height - df.select("rel_path", "digest").n_unique()
    assert repeated >= 0.9 * len(injections(preprocessed_hero, "log.duplicate"))


def test_evidence_raw_ids_survive_byte_exact(preprocessed_hero):
    answers = json.loads((preprocessed_hero.answers / "hero.json").read_text())
    ids = set(quarantine(preprocessed_hero)["raw_id"])
    for name in TABLES:
        ids |= set(table(preprocessed_hero, name)["raw_id"])
    missing = set(answers["evidence_raw_ids"]) - ids
    assert not missing, sorted(missing)[:5]


def test_ab_csv_header_is_a_row(preprocessed_hero):
    ev = table(preprocessed_hero, "events").filter(pl.col("event_kind") == "ab_allocation")
    assert ev.filter(pl.col("row_kind") == "header").height == 1
    rec = ev.filter(pl.col("row_kind") == "record")
    assert rec.height >= 1 and all(
        json.loads(f)["allocation_id"].startswith("alloc-") for f in rec["fields"])


def test_ledger_file_written(preprocessed_hero):
    led = json.loads((preprocessed_hero.proc / "ledger.json").read_text())
    assert led["coverage"]["bytes_covered"] == led["coverage"]["bytes_total"]
    assert led["quarantine"]["truncated"] >= 1 and led["tables"]["logs"] > 0


def test_cli_has_preprocess():
    args = build_parser().parse_args(["preprocess", "--scenario", "hero", "--raw", "r",
                                      "--out", "o"])
    assert (args.scenario, args.raw, args.out) == ("hero", "r", "o")
    assert callable(args.func)


@pytest.mark.parametrize("bad", ["../etc", "_answers"])
def test_cli_rejects_non_scenario_dirs(tmp_path, bad):
    (tmp_path / "_answers").mkdir()
    args = build_parser().parse_args(["preprocess", "--scenario", bad, "--raw", str(tmp_path),
                                      "--out", str(tmp_path / "o")])
    with pytest.raises(SystemExit):
        args.func(args)
