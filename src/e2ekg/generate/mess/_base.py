"""Shared types and helpers for messiness injectors.

MessInjection.detail is a JSON object (sorted keys). Record locators use
`idx` = the record's position among records of the same rel_path *at the
moment the injector ran* plus `at_utc` (ground truth, never rewritten), which
together let Task 6 tests find the record and check the undo."""
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

import numpy as np

from e2ekg.generate.formats import Stamp
from e2ekg.generate.records import RawRecord
from e2ekg.scenario.model import Scenario

Phase = Literal["structured", "bytes"]
# Tags the post-cutoff records of a fixed scenario's render while they are messed
# next to the baseline's records (generate/run.py). An injector that places a
# change by position inside a series (where it drifts, where it resets) places
# it from the other records alone, so these get it where the baseline had it.
RECOVERY_TAG = "_recovery"
Result = tuple[list[RawRecord], list["MessInjection"]]


@dataclass(frozen=True)
class MessInjection:
    injector_id: str
    rel_path: str
    detail: str
    # Whether the record(s) it corrupted carry RECOVERY_TAG: True all, False none,
    # None not said (a stream-wide injection) or a mix. Bookkeeping for the
    # recovery splice, never written to the manifest.
    on_recovery: bool | None = field(default=None, compare=False)


class Injector(Protocol):
    id: str
    sources: frozenset[str]
    phase: Phase

    def apply(self, recs: list[RawRecord], rng: np.random.Generator,
              scenario: Scenario) -> Result: ...


def _jdefault(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Stamp):
        return {"utc": o.utc.isoformat(), "style": o.style}
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, bytes):
        return o.hex()
    if isinstance(o, np.integer):
        return int(o)
    raise TypeError(f"not JSON serializable: {type(o)}")


@dataclass(frozen=True)
class FnInjector:
    id: str
    sources: frozenset[str]
    phase: Phase
    fn: Callable[["FnInjector", list[RawRecord], np.random.Generator, Scenario], Result]

    def apply(self, recs: list[RawRecord], rng: np.random.Generator,
              scenario: Scenario) -> Result:
        return self.fn(self, recs, rng, scenario)

    def eligible(self, r: RawRecord, fmts: Iterable[str] | None = None) -> bool:
        return r.source in self.sources and (fmts is None or r.fmt in fmts)

    def record(self, rel_path: str, of: Iterable[RawRecord] = (), **detail: Any) -> MessInjection:
        """`of`: the records this injection corrupted (its origin, for the splice)."""
        tagged = {RECOVERY_TAG in r.tags for r in of}
        return MessInjection(self.id, rel_path, json.dumps(
            detail, sort_keys=True, ensure_ascii=False, default=_jdefault),
            on_recovery=tagged.pop() if len(tagged) == 1 else None)


def injector(id: str, sources: Iterable[str], phase: Phase = "structured"):
    def wrap(fn) -> FnInjector:
        return FnInjector(id=id, sources=frozenset(sources), phase=phase, fn=fn)
    return wrap


def pick(rng: np.random.Generator, n: int, rate: float, minimum: int = 1) -> list[int]:
    """Sorted distinct indices: round(n*rate), but at least `minimum` when n>0."""
    if n <= 0:
        return []
    k = min(n, max(minimum, round(n * rate)))
    return sorted(int(i) for i in rng.choice(n, size=k, replace=False))


def local_index(recs: list[RawRecord]) -> list[int]:
    """Position of each record among records sharing its rel_path."""
    seen: dict[str, int] = {}
    out = []
    for r in recs:
        out.append(seen.get(r.rel_path, 0))
        seen[r.rel_path] = out[-1] + 1
    return out


def base_keys(recs: list[RawRecord], groups: dict) -> list:
    """The keys of `groups` (key -> record indices) that hold a non-recovery
    record, in order. Normally all of them; while recovery records are messed
    next to the baseline (RECOVERY_TAG), a file, stream or series only they
    have is left out, so a stream-wide injector picks over the baseline's own
    key list (the same draws, the same choices) and leaves that key alone."""
    return [k for k, idxs in groups.items()
            if any(RECOVERY_TAG not in recs[i].tags for i in idxs)]


def by_path(recs: list[RawRecord], keep: Callable[[RawRecord], bool]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for i, r in enumerate(recs):
        if keep(r):
            out.setdefault(r.rel_path, []).append(i)
    return out


def with_inserts(recs: list[RawRecord], after: dict[int, list[RawRecord]]) -> list[RawRecord]:
    """New list with after[i] inserted directly after recs[i]."""
    out: list[RawRecord] = []
    for i, r in enumerate(recs):
        out.append(r)
        out += after.get(i, ())
    return out


def set_fields(rec: RawRecord, **updates: Any) -> RawRecord:
    return replace(rec, fields={**rec.fields, **updates})


def ts_utc(v: Any) -> datetime | None:
    if isinstance(v, Stamp):
        return v.utc
    return v if isinstance(v, datetime) else None


def shift_ts(v: Any, delta: timedelta) -> Any:
    if isinstance(v, Stamp):
        return Stamp(v.utc + delta, v.style)
    if isinstance(v, datetime):
        return v + delta
    raise TypeError(f"cannot shift pre-rendered timestamp {v!r}")


# ---- app identity -------------------------------------------------------

APP_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "json": (("app",),),
    "logfmt": (("app",),),
    "atlas_jsonl": (("tags", "nf.app"), ("tags", "app"), ("tags", "service")),
    "prom": (("labels", "app"), ("labels", "service")),
    "otlp_jsonl": (("resource", "service.name"),),
    "zipkin_jsonl": (("localEndpoint", "serviceName"),),
    "spinnaker_jsonl": (("app",), ("cluster",)),
    "asg_jsonl": (("app",),),
    "properties_log": (("app",),),
    "radar_jsonl": (("app",), ("service",)),
    "pagerduty_jsonl": (("app",), ("service",)),
}


def app_ref(r: RawRecord) -> tuple[tuple[str, ...], str] | None:
    for path in APP_PATHS.get(r.fmt, ()):
        node: Any = r.fields
        for k in path:
            node = node.get(k) if isinstance(node, dict) else None
        if isinstance(node, str):
            return path, node
    return None


def set_path(fields: dict, path: tuple[str, ...], value: Any) -> dict:
    """Copy-on-write update of a nested key."""
    head, *rest = path
    new = dict(fields)
    new[head] = set_path(fields[head], tuple(rest), value) if rest else value
    return new


def rename_key(d: dict, old: str, new: str, value: Any = None) -> dict:
    """Same key position, new name (and optionally a new value)."""
    return {(new if k == old else k): (v if k != old or value is None else value)
            for k, v in d.items()}


_SUFFIX = re.compile(r"(-(prod|v\d+|[a-z0-9]{8,10}-[a-z0-9]{5}))+$")


def resolver(scenario: Scenario) -> Callable[[str], str | None]:
    """Generation-side truth: surface form -> catalog id (for injectors that
    must recognise an app after the id.* injectors rewrote it)."""
    table: dict[str, str] = {}
    for a in scenario.catalog:
        for s in (a.id, *a.aliases):
            table[s.lower()] = a.id

    def resolve(surface: str) -> str | None:
        s = surface.lower()
        for cand in (s, _SUFFIX.sub("", s)):
            if cand in table:
                return table[cand]
        return None
    return resolve


def word_re(term: str) -> re.Pattern:
    return re.compile(rf"(?<![\w-]){re.escape(term)}(?![\w-])")
