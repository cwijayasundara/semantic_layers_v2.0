"""Log templates in the as-of view are mined from the lines observable at the cutoff, so
lines after it cannot reshape them (R21, R34)."""
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from e2ekg.preprocess.asof import scan
from e2ekg.preprocess.logtemplates import mine_templates

T0 = datetime(2026, 1, 1, tzinfo=UTC)
CUT = T0 + timedelta(minutes=30)
LOG_TABLES = ("logs", "derived/rows_logs", "derived/fp_log_buckets", "derived/fp_log_spikes")


def _lines(post: list[tuple[int, str, str]]) -> list[tuple[datetime, str, str]]:
    """Steady background traffic over an hour, a burst and a one-off line before the cutoff,
    and `post` (second offset, level, msg) after it."""
    rows = [(T0 + timedelta(seconds=s), "INFO", f"request served in {s % 97} ms")
            for s in range(0, 3600, 10)]
    rows += [(T0 + timedelta(minutes=15, seconds=s), "ERROR", "connection reset by peer alpha")
             for s in range(30)]
    rows.append((T0 + timedelta(minutes=20), "INFO", "user alice logged in from paris"))
    rows += [(CUT + timedelta(seconds=s), lv, m) for s, lv, m in post]
    return sorted(rows)


def _tree(root: Path, post: list[tuple[int, str, str]]) -> Path:
    """A processed tree holding only logs, written as the fingerprint step writes them."""
    proc = root / "proc"
    (proc / "derived").mkdir(parents=True)
    rows = _lines(post)
    logs = pl.DataFrame({
        "raw_id": [f"logs|logs/a/i-1.log|{i * 100}|99|{i:016x}" for i in range(len(rows))],
        "app_uri": ["https://kg.local/app/a"] * len(rows),
        "ts_utc": pl.Series([t for t, _, _ in rows], dtype=pl.Datetime("us", "UTC")),
        "level": [lv for _, lv, _ in rows], "msg": [m for _, _, m in rows]})
    logs = mine_templates(logs)
    logs.select("raw_id", "app_uri", "ts_utc", pl.col("template_id").alias("key")).write_parquet(
        proc / "derived" / "rows_logs.parquet")
    logs.write_parquet(proc / "logs.parquet")
    return proc


@pytest.fixture(scope="module")
def trees(tmp_path_factory):
    plain = _tree(tmp_path_factory.mktemp("a"), [(60, "INFO", "request served in 5 ms")])
    reshaped = _tree(tmp_path_factory.mktemp("b"), [
        (60, "INFO", "request served in 5 ms"),
        *((120 + s, "ERROR", "connection reset by peer beta") for s in range(3)),
        (300, "INFO", "user bob logged in from berlin")])
    return plain, reshaped


def _templates(proc: Path) -> set[str]:
    return set(pl.read_parquet(proc / "logs.parquet")["template"])


def test_post_cutoff_lines_reshape_the_full_window_templates(trees):
    """The premise: over the whole window the later lines do change the templates."""
    plain, reshaped = trees
    assert _templates(plain) != _templates(reshaped)


@pytest.mark.parametrize("name", LOG_TABLES)
def test_asof_templates_ignore_post_cutoff_lines(trees, name):
    plain, reshaped = trees
    a = scan(plain, name, CUT).collect()
    b = scan(reshaped, name, CUT).collect()
    assert a.height > 0
    assert a.equals(b)


def test_asof_templates_are_the_observable_lines_own(trees):
    """Mined over the lines before the cutoff, with the code preprocessing uses."""
    _, reshaped = trees
    got = scan(reshaped, "logs", CUT).collect()
    seen = pl.read_parquet(reshaped / "logs.parquet").filter(pl.col("ts_utc") < CUT)
    want = mine_templates(seen.drop("template_id", "template", "params"))
    assert got.columns == seen.columns
    assert got.select("raw_id", "template_id", "template", "params").equals(
        want.select("raw_id", "template_id", "template", "params"))
    keys = scan(reshaped, "derived/rows_logs", CUT).collect()
    assert keys.select("raw_id", pl.col("key").alias("template_id")).equals(
        want.select("raw_id", "template_id"))
    spikes = scan(reshaped, "derived/fp_log_spikes", CUT).collect()
    assert spikes["template"].to_list() == ["connection reset by peer alpha"]


def test_concurrent_first_requests_mine_once(tmp_path, monkeypatch):
    """The cache holds every scenario, and two first requests for one key mine it once."""
    import threading
    import time

    from e2ekg.preprocess import asof
    assert asof._mine.cache_info().maxsize >= 7
    path = tmp_path / "logs.parquet"
    pl.DataFrame({"raw_id": ["a", "b"], "msg": ["x 1", "x 2"],
                  "ts_utc": [T0, T0 + timedelta(seconds=1)]}).write_parquet(path)
    calls = []

    def slow(df):
        calls.append(1)
        time.sleep(0.3)
        return mine_templates(df)

    monkeypatch.setattr(asof, "mine_templates", slow)
    st = path.stat()
    key = (path, (st.st_mtime_ns, st.st_size), CUT)
    out = []
    threads = [threading.Thread(target=lambda: out.append(asof._mined(*key))) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(calls) == 1 and len(out) == 4
