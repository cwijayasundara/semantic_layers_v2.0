import json
import shutil
import subprocess
import sys

import polars as pl
import pytest
from pp_helpers import table

from e2ekg.preprocess.fingerprints import load_fingerprints
from e2ekg.preprocess.ledger import ReachabilityReport, check_reachability
from e2ekg.preprocess.tables import TABLES


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "e2ekg.cli", *args], capture_output=True,
                          text=True)


@pytest.fixture
def proc_copy(preprocessed_hero, tmp_path):
    dst = tmp_path / "hero"
    shutil.copytree(preprocessed_hero.proc, dst)
    return dst


def test_every_parsed_row_reachable(preprocessed_hero):
    r = check_reachability(preprocessed_hero.proc, load_fingerprints(preprocessed_hero.proc))
    assert isinstance(r, ReachabilityReport)
    assert r.unreachable_raw_ids == [] and r.rows_reachable == r.rows_total
    assert r.rows_total == sum(table(preprocessed_hero, t).height for t in TABLES)
    assert r.phantom_raw_ids == []


def test_dropping_a_fingerprint_leaves_rows_unreachable(preprocessed_hero):
    fps = load_fingerprints(preprocessed_hero.proc)
    fps["fp_log_buckets"] = fps["fp_log_buckets"].slice(1)
    fps["fp_log_spikes"] = fps["fp_log_spikes"].clear()
    r = check_reachability(preprocessed_hero.proc, fps)
    first = fps["fp_log_buckets"].height
    assert r.unreachable_raw_ids and r.rows_reachable < r.rows_total and first > 0
    assert all(i.startswith("logs|") for i in r.unreachable_raw_ids)


def test_direct_node_tables_need_no_fingerprint(preprocessed_hero):
    fps = {k: v.clear() for k, v in load_fingerprints(preprocessed_hero.proc).items()}
    r = check_reachability(preprocessed_hero.proc, fps)
    direct = sum(table(preprocessed_hero, t).height for t in
                 ("events", "alerts", "pages", "incidents", "catalog", "slack"))
    assert r.rows_reachable == direct


def test_ledger_cli_passes_on_intact_output(preprocessed_hero, proc_copy):
    p = run_cli("ledger", "--proc", str(proc_copy), "--raw", str(preprocessed_hero.raw))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "reachable" in p.stdout and "coverage" in p.stdout


def test_ledger_cli_fails_on_deleted_row(proc_copy):
    lg = pl.read_parquet(proc_copy / "logs.parquet")
    lg.slice(1).write_parquet(proc_copy / "logs.parquet")
    p = run_cli("ledger", "--proc", str(proc_copy))
    assert p.returncode != 0
    assert "FAIL" in p.stdout


def test_ledger_cli_fails_when_a_bucket_is_missing(proc_copy):
    f = proc_copy / "derived" / "fp_edges.parquet"
    pl.read_parquet(f).slice(1).write_parquet(f)
    assert run_cli("ledger", "--proc", str(proc_copy)).returncode != 0


def test_ledger_cli_scenario_flag(preprocessed_hero):
    out = preprocessed_hero.proc.parent
    p = run_cli("ledger", "--scenario", "hero", "--out", str(out), "--raw",
                str(preprocessed_hero.raw.parent))
    assert p.returncode == 0, p.stdout + p.stderr
    assert json.loads((preprocessed_hero.proc / "ledger.json").read_text())["tables"]


def test_derived_passes_keep_file_row_order(preprocessed_hero):
    """Regression: the identity and fingerprint passes rewrite logs/spans with lazy joins;
    an unordered join shuffled rows, so 'drop the first row' hit an arbitrary record and
    test_ledger_reports_gaps_and_overlaps saw a gap at offset 59 instead of 0. Rows must
    stay in file order (offsets ascending within a file)."""
    for t in ("logs", "spans", "metrics_atlas"):
        df = table(preprocessed_hero, t).select("rel_path", "raw_id")
        off = df.with_columns(pl.col("raw_id").str.extract(r"\|(\d+)\|\d+\|[0-9a-f]{16}$", 1)
                              .cast(pl.Int64).alias("o"))
        bad = off.filter((pl.col("rel_path") == pl.col("rel_path").shift(1))
                         & (pl.col("o") < pl.col("o").shift(1)))
        assert bad.height == 0, t
