import json
import os
import subprocess
import sys

import pytest
from helpers import (
    jsonl,
    load_answers,
    manifest,
    npe_rate,
    service_mentions,
    slack_messages,
    slack_stats,
    tree_hash,
    ttr_delta,
)

from e2ekg.cli import main
from e2ekg.generate.hero_chat import HERO_SERVICES
from e2ekg.generate.run import generate_scenario, render_unmessed
from e2ekg.rawid import RawSpan
from e2ekg.scenario.hero import hero_scenario

HERO_CHANNEL = "inc-5377-homepage-ttr"


def test_generate_is_byte_deterministic(generated_hero, tmp_path):
    generate_scenario(hero_scenario(), tmp_path / "b" / "hero", scale=0.05)
    assert tree_hash(generated_hero) == tree_hash(tmp_path / "b" / "hero")
    a = (generated_hero.parent / "_answers" / "hero.json").read_bytes()
    assert a == (tmp_path / "b" / "_answers" / "hero.json").read_bytes()


def test_generate_is_deterministic_across_processes(tmp_path):
    """Two fresh interpreters with different hash seeds produce the same tree."""
    procs = []
    for k, out in enumerate(("p1", "p2")):
        env = {**os.environ, "PYTHONHASHSEED": str(k + 1)}
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "e2ekg.cli", "generate", "--scenario", "hero", "--scale",
             "0.05", "--out", str(tmp_path / out)], env=env, stdout=subprocess.DEVNULL))
    assert [p.wait(timeout=600) for p in procs] == [0, 0]
    assert tree_hash(tmp_path / "p1" / "hero") == tree_hash(tmp_path / "p2" / "hero")
    for name in ("hero.json", "hero.manifest.json"):
        assert ((tmp_path / "p1" / "_answers" / name).read_bytes()
                == (tmp_path / "p2" / "_answers" / name).read_bytes())


def test_generate_refuses_non_empty_out(tmp_path):
    (tmp_path / "hero").mkdir()
    (tmp_path / "hero" / "stale.txt").write_text("x")
    with pytest.raises(FileExistsError):
        generate_scenario(hero_scenario(), tmp_path / "hero", scale=0.05)


@pytest.mark.slow
def test_hero_sizes(tmp_path):
    out = tmp_path / "hero"
    generate_scenario(hero_scenario(), out, scale=1.0)
    m = manifest(out)
    assert 2_000_000 <= m.counts["logs"] <= 5_000_000
    assert m.counts["spans"] >= 150_000
    assert slack_stats(out, HERO_CHANNEL) == (200, 50, 15)


def test_hero_slack_channel_stats(generated_hero):
    assert slack_stats(generated_hero, HERO_CHANNEL) == (200, 50, 15)
    texts = [m["text"] for m in slack_messages(generated_hero, HERO_CHANNEL)]
    assert service_mentions(generated_hero, texts) == set(HERO_SERVICES)


def test_ttr_regression_cross_platform_and_cell_independent(generated_hero):
    for p in ("tv", "android", "ios", "web"):
        assert ttr_delta(generated_hero, p) == pytest.approx(1.5, abs=0.2)
    assert abs(ttr_delta(generated_hero, "android", cell="B")
               - ttr_delta(generated_hero, "android", cell="A")) < 0.1


def test_answer_key_written_outside_raw_tree(generated_hero):
    assert not any("_answers" in str(p) for p in generated_hero.rglob("*"))
    assert not any("manifest" in p.name for p in generated_hero.rglob("*"))
    ans = load_answers("hero", generated_hero.parent)
    assert ans["answer_key"]["root_cause_change_id"] == "chg-cfg-0001"
    assert len(ans["evidence_raw_ids"]) >= 8


def test_chronic_npe_flat(generated_hero):
    assert npe_rate(generated_hero, "12:00", "14:00") == pytest.approx(
        npe_rate(generated_hero, "15:00", "17:00"), rel=0.15)
    assert npe_rate(generated_hero, "12:00", "14:00") > 0


def test_evidence_ids_point_at_their_bytes(generated_hero):
    ans = load_answers("hero", generated_hero.parent)
    tags = set()
    for rid in ans["evidence_raw_ids"]:
        s = RawSpan.parse(rid)
        with (generated_hero / s.rel_path).open("rb") as fh:
            fh.seek(s.offset)
            payload = fh.read(s.length)
        assert len(payload) == s.length
        import hashlib
        assert rid.endswith(hashlib.sha256(payload).hexdigest()[:16])
    for sig, ids in ans["evidence_by_tag"].items():
        assert ids, sig
        tags.add(sig)
    assert tags == set(ans["answer_key"]["evidence_tags"])


def test_manifest_lists_files_counts_and_trace_population(generated_hero):
    m = manifest(generated_hero)
    files = {p.relative_to(generated_hero).as_posix() for p in generated_hero.rglob("*")
             if p.is_file()}
    assert set(m.files) == files
    pop = m.trace_population
    assert pop["traces_considered"] > 10 * pop["traces_kept_head"]
    assert pop["traces_kept"] == pop["traces_kept_head"] + pop["traces_kept_error"]
    assert m.counts["spans"] > 0 and m.counts["logs"] > 0
    assert {i["injector_id"] for i in m.injections} >= {"log.truncate", "trc.zipkin_legacy"}


def test_cli_generates_a_variant(tmp_path):
    assert main(["generate", "--scenario", "v-control", "--out", str(tmp_path),
                 "--scale", "0.01"]) == 0
    assert (tmp_path / "v-control" / "catalog" / "apps.yaml").exists()
    ans = json.loads((tmp_path / "_answers" / "v-control.json").read_text())
    assert ans["answer_key"]["root_cause_change_id"] is None
    assert ans["evidence_raw_ids"] == []
    assert jsonl(tmp_path / "v-control" / "metrics" / "atlas.jsonl")


def test_render_unmessed_matches_family_subsets_and_includes_chat_beats():
    """The public pre-mess render: a family subset is the same as the full render's
    (each renderer owns its rng), and chat beats come with their Slack records."""
    s = hero_scenario()
    full = render_unmessed(s, scale=0.05)
    part = render_unmessed(s, scale=0.05, families=("events", "chat"))
    assert part.records["events"] == full.records["events"] and part.records["events"]
    assert part.records["metrics"] == [] and part.records["logs"] == []
    assert part.beats == full.beats and len(part.records["chat"]) == len(part.beats) > 0
    assert full.all_records()[:len(full.records["catalog"])] == full.records["catalog"]
