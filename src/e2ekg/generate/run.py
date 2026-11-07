"""generate_scenario: render every signal from one Scenario, apply the mess,
write the raw tree, and keep generator-only truth outside it (R5):
`<out>/../_answers/<name>.json` (answer key, `as_of_utc` cutoff (R21),
`external_ids` raw-id -> scenario change id map (R22), evidence raw ids) and
`<out>/../_answers/<name>.manifest.json` (injections, files, counts).

A scenario with a fix (`fault.fix_change_id`) recovers after it. Nothing at
or before `as_of_utc` may change because of that (the agent's inputs). The
renderers keep their own streams drawing exactly as without the fix (recovery
draws come from `world.recovery_rng`), but `apply_mess` draws one rng over
every record, so a post-cutoff difference in record counts would still
reshuffle the mess everywhere. So a fixed scenario is generated from two
renders: its baseline (`baseline_scenario`: the fix and every change at or
after it removed), messed exactly as a plain run, gives every record up to
the cutoff; the fixed render's post-cutoff records are messed *alongside* the
baseline's records with the baseline's per-injector rng states replayed, so
each file-, stream- or series-wide corruption (aliases, units, time zones,
host skew, ...) lands on them as it did on the baseline, and only they are
kept (a file, stream or series only they have is left out of the stream-wide
injectors' key lists, `mess._base.base_keys`). Per file: the baseline's
records up to its first post-cutoff record, then the fixed records, then the
baseline's remaining pre-cutoff records.

Those last ones are met.late's late arrivals: Atlas points stamped before the
cutoff that the mess appends after everything else in metrics/atlas.jsonl.
The as-of view keeps them (it filters on the point's own `t`), and in a fixed
scenario their bytes and times are unchanged but their offsets, so their raw
ids, are not: they come after post-cutoff data that the fix changed. This is
accepted: the agent's graph (harvest.ttl) carries no Atlas raw ids. Every
other record at or before the cutoff keeps its raw id
(tests/generate/test_recovery.py checks both)."""
import json
import zlib
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from e2ekg.generate.mess import apply_mess
from e2ekg.generate.mess._base import RECOVERY_TAG, MessInjection
from e2ekg.generate.records import RawRecord, write_source
from e2ekg.generate.render_catalog import render_catalog
from e2ekg.generate.render_chat import Beat, beat_records, chat_beats
from e2ekg.generate.render_events import external_ids, render_events
from e2ekg.generate.render_logs import render_logs
from e2ekg.generate.render_metrics import render_metrics
from e2ekg.generate.render_traces import Population, render_traces
from e2ekg.generate.world import RAMP_S, as_of_utc, fix_change
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
    fix = fix_change(scenario)
    degs = propagate(scenario.fault, scenario.edges, scenario.window,
                     fixed_utc=fix.at_utc if fix is not None else None)
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


def baseline_scenario(scenario: Scenario) -> Scenario:
    """The scenario as it was before its fix was added: no fix, and none of the
    changes at or after the fix (the fix itself and the recovery it causes)."""
    fix = fix_change(scenario)
    if fix is None:
        return scenario
    return scenario.model_copy(update={
        "fault": scenario.fault.model_copy(update={"fix_change_id": None}),
        "changes": tuple(c for c in scenario.changes if c.at_utc < fix.at_utc),
        "answer_key": scenario.answer_key.model_copy(
            update={"fix_change_id": None, "recovered_utc": None})})


def _render_recovered(scenario: Scenario, scale: float, cut: datetime
                      ) -> tuple[RenderedScenario, list[RawRecord], list[MessInjection]]:
    """See the module docstring: baseline records up to the cutoff, the fixed
    render's after it, messed consistently with each other."""
    base = baseline_scenario(scenario)
    b = render_unmessed(base, scale)
    base_recs = b.all_records()
    b.records = {}
    states: list[dict] = []
    base_messy, base_inj = apply_mess(base_recs, b.mess_rng, base, states=states)
    base_paths = {r.rel_path for r in base_messy if _is_pre(r, cut)}
    head, late = _split_base(base_messy, cut)
    del base_messy
    f = render_unmessed(scenario, scale)
    post = [replace(r, tags=r.tags | {RECOVERY_TAG}) for r in f.all_records()
            if not _is_pre(r, cut)]
    f.records = {}
    mixed, mixed_inj = apply_mess(base_recs + post, f.mess_rng, scenario, states=states)
    del base_recs, post
    fixed = [replace(r, tags=r.tags - {RECOVERY_TAG}) for r in mixed if RECOVERY_TAG in r.tags]
    del mixed
    injections = _splice_injections(base_inj, mixed_inj, cut, base_paths,
                                    {r.rel_path for r in fixed})
    return f, head + fixed + late, injections


def _is_pre(r: RawRecord, cut: datetime) -> bool:
    return r.at_utc is None or r.at_utc <= cut


def _split_base(base: list[RawRecord], cut: datetime
                ) -> tuple[list[RawRecord], list[RawRecord]]:
    """The baseline's pre-cutoff records: those before each file's first
    post-cutoff record, and those after it (late arrivals), in order."""
    head: list[RawRecord] = []
    late: list[RawRecord] = []
    seen_post: set[str] = set()
    for r in base:
        if not _is_pre(r, cut):
            seen_post.add(r.rel_path)
        elif r.rel_path in seen_post:
            late.append(r)
        else:
            head.append(r)
    return head, late


def _splice_injections(base: list[MessInjection], fixed: list[MessInjection],
                       cut: datetime, base_paths: set[str],
                       fixed_paths: set[str]) -> list[MessInjection]:
    """The baseline's injections on records up to the cutoff and the mixed run's
    after it (some of those were on baseline records the splice drops); one with
    no record time goes with the run whose records its file holds (once, if both
    made the same one up to its count)."""
    def when(i: MessInjection) -> datetime | None:
        d = json.loads(i.detail)
        at = d.get("at_utc", d.get("at_utc_a"))
        if isinstance(at, list):
            at = at[0] if at else None
        return datetime.fromisoformat(at) if isinstance(at, str) else None

    def key(i: MessInjection) -> tuple[str, str, str]:
        d = json.loads(i.detail)
        d.pop("count", None)
        return i.injector_id, i.rel_path, json.dumps(d, sort_keys=True)

    out, seen = [], set()
    for i in base:
        at = when(i)
        if (at is None and i.rel_path in base_paths) or (at is not None and at <= cut):
            out.append(i)
            seen.add(key(i))
    for i in fixed:
        at = when(i)
        if (at is None and i.rel_path in fixed_paths and key(i) not in seen) or \
                (at is not None and at > cut):
            out.append(i)
    return out


def recovered_utc(scenario: Scenario, degs: tuple[Degradation, ...]) -> datetime | None:
    """When the last degradation has fully ended (the answer key's value if set)."""
    if scenario.answer_key.recovered_utc is not None:
        return scenario.answer_key.recovered_utc
    ends = [d.end_utc for d in degs if d.end_utc is not None]
    return max(ends) + timedelta(seconds=RAMP_S) if ends else None


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


def _z(at: datetime | None) -> str | None:
    return at.isoformat().replace("+00:00", "Z") if at else None


def generate_scenario(scenario: Scenario, out: Path, scale: float = 1.0) -> GenerationManifest:
    out = Path(out)
    _check_empty(out)
    cut = as_of_utc(scenario)
    fix = fix_change(scenario)
    if fix is not None and (cut is None or fix.at_utc <= cut):
        raise ValueError(f"{scenario.name}: the fix {fix.id} must land after the cutoff {cut}")
    if fix is None:
        rendered = render_unmessed(scenario, scale)
        recs = rendered.all_records()
        rendered.records = {}
        messy, injections = apply_mess(recs, rendered.mess_rng, scenario)
        del recs
    else:
        rendered, messy, injections = _render_recovered(scenario, scale, cut)
    pop = rendered.population
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
    recovered = recovered_utc(scenario, rendered.degradations)
    answers = {"name": scenario.name, "seed": scenario.seed,
               "answer_key": scenario.answer_key.model_dump(mode="json"),
               "as_of_utc": _z(cut),
               "fix_change_id": scenario.fault.fix_change_id,
               "recovered_utc": _z(recovered),
               "external_ids": external_ids(scenario),
               "evidence_raw_ids": evidence, "evidence_by_tag": by_tag}
    adir = out.parent / ANSWERS_DIR
    adir.mkdir(parents=True, exist_ok=True)
    (adir / f"{scenario.name}.json").write_text(json.dumps(answers, indent=1, sort_keys=True))
    (adir / f"{scenario.name}.manifest.json").write_text(
        json.dumps(asdict(manifest), sort_keys=True))
    return manifest
