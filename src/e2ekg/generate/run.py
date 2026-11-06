"""generate_scenario: render every signal from one Scenario, apply the mess,
write the raw tree, and keep generator-only truth outside it (R5):
`<out>/../_answers/<name>.json` (answer key + evidence raw ids) and
`<out>/../_answers/<name>.manifest.json` (injections, files, counts)."""
import json
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from e2ekg.generate.mess import apply_mess
from e2ekg.generate.records import RawRecord, write_source
from e2ekg.generate.render_catalog import render_catalog
from e2ekg.generate.render_chat import render_chat
from e2ekg.generate.render_events import render_events
from e2ekg.generate.render_logs import render_logs
from e2ekg.generate.render_metrics import render_metrics
from e2ekg.generate.render_traces import Population, render_traces
from e2ekg.scenario.model import Scenario
from e2ekg.scenario.propagation import propagate

ANSWERS_DIR = "_answers"


@dataclass
class GenerationManifest:
    name: str
    seed: int
    scale: float
    files: list[str]
    counts: dict[str, int]
    trace_population: dict
    injections: list[dict]


def _counts(recs: list[RawRecord]) -> dict[str, int]:
    c: dict[str, int] = {}
    for r in recs:
        c[r.source] = c.get(r.source, 0) + 1
        if r.source == "logs":
            c["log_lines"] = c.get("log_lines", 0) + r.payload.count(b"\n") + 1
        elif r.source == "traces":
            n = len(r.fields["spans"]) if r.fmt == "otlp_jsonl" else 1
            c["spans"] = c.get("spans", 0) + n
    return dict(sorted(c.items()))


def _check_empty(out: Path) -> None:
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FileExistsError(f"{out} must not exist or be empty")


def generate_scenario(scenario: Scenario, out: Path, scale: float = 1.0) -> GenerationManifest:
    out = Path(out)
    _check_empty(out)
    degs = propagate(scenario.fault, scenario.edges, scenario.window)
    seq = np.random.SeedSequence([scenario.seed, zlib.crc32(scenario.name.encode())])
    r_cat, r_met, r_log, r_trc, r_evt, r_chat, r_mess = (
        np.random.default_rng(s) for s in seq.spawn(7))
    pop = Population()
    recs = render_catalog(scenario, degs, r_cat)
    recs += render_metrics(scenario, degs, r_met, scale=scale)
    recs += render_logs(scenario, degs, r_log, scale=scale)
    recs += render_traces(scenario, degs, r_trc, scale=scale, population=pop)
    recs += render_events(scenario, degs, r_evt)
    recs += render_chat(scenario, degs, r_chat)
    messy, injections = apply_mess(recs, r_mess, scenario)
    del recs
    counts = _counts(messy)
    out.mkdir(parents=True, exist_ok=True)
    ids = write_source(messy, out)  # one call: every rel_path is created exactly once
    files = sorted({r.rel_path for r in messy})
    del messy
    by_tag: dict[str, list[str]] = {t: [] for t in scenario.answer_key.evidence_tags}
    evidence = []
    for rid, tg in ids:
        ev = sorted(t for t in tg if t.startswith("evidence:"))
        if ev:
            evidence.append(rid)
            for t in ev:
                by_tag.setdefault(t, []).append(rid)
    manifest = GenerationManifest(
        name=scenario.name, seed=scenario.seed, scale=scale, files=files, counts=counts,
        trace_population=pop.as_dict(),
        injections=[{"injector_id": i.injector_id, "rel_path": i.rel_path, "detail": i.detail}
                    for i in injections])
    answers = {"name": scenario.name, "seed": scenario.seed,
               "answer_key": scenario.answer_key.model_dump(mode="json"),
               "evidence_raw_ids": evidence, "evidence_by_tag": by_tag}
    adir = out.parent / ANSWERS_DIR
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"{scenario.name}.json").write_text(json.dumps(answers, indent=1, sort_keys=True))
    (adir / f"{scenario.name}.manifest.json").write_text(
        json.dumps(asdict(manifest), sort_keys=True))
    return manifest
