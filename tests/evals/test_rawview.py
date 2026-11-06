"""The raw tree as observable at the cutoff: the raw_only arm may not read what the graph
arms cannot."""
import json

import polars as pl
import pytest

from e2ekg.evals.rawview import build_raw_view, ensure_raw_view
from e2ekg.preprocess import asof


@pytest.fixture(scope="module")
def view(preprocessed_hero, tmp_path_factory):
    cut = asof.live_cutoff(preprocessed_hero.proc).as_of
    dest = tmp_path_factory.mktemp("view") / "hero"
    return dest, cut, build_raw_view(preprocessed_hero.raw, preprocessed_hero.proc, cut, dest)


def _parse(raw_id: str) -> tuple[str, int, int]:
    _, path, off, n, _ = raw_id.split("|")
    return path, int(off), int(off) + int(n)


def _covered(view_map: dict, path: str, lo: int, hi: int) -> bool:
    return any(o < hi and lo < o + n for _, o, n in view_map.get(path, ()))


@pytest.mark.parametrize("table", ["slack", "incidents", "alerts", "events"])
def test_a_record_stamped_before_the_cutoff_is_in_the_view_and_one_after_it_is_not(
        view, preprocessed_hero, table):
    _, cut, segs = view
    rows = asof.scan(preprocessed_hero.proc, table).select("raw_id", "ts_utc").collect()
    before = rows.filter(pl.col("ts_utc") < cut)["raw_id"]
    after = rows.filter(pl.col("ts_utc") >= cut)["raw_id"]
    assert before.len() and after.len(), f"{table}: the cutoff does not split it"
    assert all(_covered(segs, *_parse(r)[:1], *_parse(r)[1:]) for r in before)
    assert not any(_covered(segs, p, lo, hi) for p, lo, hi in map(_parse, after))


def test_the_view_holds_the_original_bytes_in_order_and_the_map_finds_them(view, preprocessed_hero):
    dest, _, segs = view
    for path, segments in segs.items():
        got, orig = (dest / path).read_bytes(), (preprocessed_hero.raw / path).read_bytes()
        assert len(got) == sum(n for _, _, n in segments) <= len(orig)
        for v0, o0, n in segments:
            assert got[v0:v0 + n] == orig[o0:o0 + n]


def test_the_catalog_is_kept_whole_and_the_view_is_smaller_than_the_tree(view, preprocessed_hero):
    dest, _, segs = view
    assert (dest / "catalog/apps.yaml").read_bytes() == (
        preprocessed_hero.raw / "catalog/apps.yaml").read_bytes()
    size = lambda root: sum(p.stat().st_size for p in root.rglob("*") if p.is_file())  # noqa: E731
    assert size(dest) < size(preprocessed_hero.raw)


def test_a_view_cut_at_the_same_instant_is_reused_and_another_is_rebuilt(
        view, preprocessed_hero, monkeypatch):
    dest, cut, segs = view
    from e2ekg.evals import rawview
    monkeypatch.setattr(rawview, "build_raw_view", lambda *a: pytest.fail("rebuilt"))
    assert ensure_raw_view(preprocessed_hero.raw, preprocessed_hero.proc, cut, dest) == segs
    monkeypatch.setattr(rawview, "build_raw_view", lambda *a: "rebuilt")
    assert ensure_raw_view(preprocessed_hero.raw, preprocessed_hero.proc,
                           cut.replace(hour=cut.hour - 1), dest) == "rebuilt"
    json.loads(dest.with_name("hero.view.json").read_text())


def test_records_keep_their_line_breaks_so_a_view_line_is_still_one_record(view, preprocessed_hero):
    dest, cut, _ = view
    for table, path in (("alerts", "alerts/radar.jsonl"), ("incidents", "incidents/incident_io.jsonl"),
                        ("slack", None)):
        rows = asof.scan(preprocessed_hero.proc, table, cut).select("raw_id").unique().collect()
        files = {r.split("|")[1] for r in rows["raw_id"]}
        for f in files:
            lines = (dest / f).read_bytes().splitlines()
            n = sum(r.split("|")[1] == f for r in rows["raw_id"])
            assert len(lines) == n, f
            if f.endswith(".jsonl"):
                assert all(json.loads(line) for line in lines), f
