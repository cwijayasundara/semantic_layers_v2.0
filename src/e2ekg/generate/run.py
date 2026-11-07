"""generate_scenario: render every signal from one Scenario, apply the mess,
write the raw tree, and keep generator-only truth outside it (R5):
`<out>/../_answers/<name>.json` (answer key, `as_of_utc` cutoff (R21),
`external_ids` raw-id -> scenario change id map (R22), evidence raw ids) and
`<out>/../_answers/<name>.manifest.json` (injections, files, counts)."""
import json
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from e2ekg.generate.mess import apply_mess
from e2ekg.generate.records import RawRecord, write_source
from e2ekg.generate.render_catalog import render_catalog
from e2ekg.generate.render_chat import Beat, beat_records, chat_beats
from e2ekg.generate.render_events import external_ids, render_events
from e2ekg.generate.render_logs import render_logs
from e2ekg.generate.render_metrics import render_metrics
from e2ekg.generate.render_traces import Population, render_traces
from e2ekg.generate.world import as_of_utc
from e2ekg.scenario.model import Scenario
from e2ekg.scenario.propagation import Degradation, propagate

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


FAMILIES = ("catalog", "metrics", "logs", "traces", "events", "chat")
# One rng per renderer, spawned from SeedSequence([seed, crc32(name)]) in this
# order; the last one belongs to the mess and is shared by every injector.
_RNG_ORDER = (*FAMILIES, "mess")


@dataclass
class RenderedScenario:
    """Everything `generate_scenario` renders before the mess. `records` holds
    the RawRecords per family (families not asked for are empty); `beats` is
    the chat family before it is turned into Slack records."""
    scenario: Scenario
    degradations: tuple[Degradation, ...]
    records: dict[str, list[RawRecord]]
    beats: list[Beat]
    population: Population
    mess_rng: np.random.Generator = field(repr=False)

    def all_records(self) -> list[RawRecord]:
        return [r for f in FAMILIES for r in self.records[f]]


def render_unmessed(scenario: Scenario, scale: float = 1.0,
                    families: tuple[str, ...] = FAMILIES) -> RenderedScenario:
    """Render the scenario's signals before `apply_mess`, with exactly the rng
    stream each renderer gets in `generate_scenario`. `families` limits the work
    to the named renderers (events and chat do not depend on `scale`)."""
    degs = propagate(scenario.fault, scenario.edges, scenario.window)
    seq = np.random.SeedSequence([scenario.seed, zlib.crc32(scenario.name.encode())])
    rng = dict(zip(_RNG_ORDER, (np.random.default_rng(s) for s in seq.spawn(len(_RNG_ORDER))),
                   strict=True))
    pop = Population()
    want = set(families)
    recs: dict[str, list[RawRecord]] = {f: [] for f in FAMILIES}
    beats: list[Beat] = []
    if "catalog" in want:
        recs["catalog"] = render_catalog(scenario, degs, rng["catalog"])
    if "metrics" in want:
        recs["metrics"] = render_metrics(scenario, degs, rng["metrics"], scale=scale)
    if "logs" in want:
        recs["logs"] = render_logs(scenario, degs, rng["logs"], scale=scale)
    if "traces" in want:
        recs["traces"] = render_traces(scenario, degs, rng["traces"], scale=scale,
                                       population=pop)
    if "events" in want:
        recs["events"] = render_events(scenario, degs, rng["events"])
    if "chat" in want:
        beats = chat_beats(scenario, rng["chat"])
        recs["chat"] = beat_records(beats)
    return RenderedScenario(scenario, degs, recs, beats, pop, rng["mess"])


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
    rendered = render_unmessed(scenario, scale)
    pop, recs = rendered.population, rendered.all_records()
    rendered.records = {}
    messy, injections = apply_mess(recs, rendered.mess_rng, scenario)
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
    cut = as_of_utc(scenario)
    answers = {"name": scenario.name, "seed": scenario.seed,
               "answer_key": scenario.answer_key.model_dump(mode="json"),
               "as_of_utc": cut.isoformat().replace("+00:00", "Z") if cut else None,
               "external_ids": external_ids(scenario),
               "evidence_raw_ids": evidence, "evidence_by_tag": by_tag}
    adir = out.parent / ANSWERS_DIR
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"{scenario.name}.json").write_text(json.dumps(answers, indent=1, sort_keys=True))
    (adir / f"{scenario.name}.manifest.json").write_text(
        json.dumps(asdict(manifest), sort_keys=True))
    return manifest
