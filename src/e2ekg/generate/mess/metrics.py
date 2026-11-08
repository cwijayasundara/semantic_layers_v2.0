"""met.* injectors. A series is (name, sorted tags/labels); Atlas points carry
the value in `v`, Prometheus samples in `value`. One MessInjection per
(series, rel_path) touched."""
import json
from dataclasses import replace

from e2ekg.generate.mess._base import (
    RECOVERY_TAG,
    base_keys,
    injector,
    pick,
    rename_key,
    with_inserts,
)

DRIFT_RATE, UNIT_RATE, RESET_RATE, GAP_RATE, LATE_RATE, DUP_RATE = 0.10, 0.2, 0.1, 0.05, 0.05, 0.02
DRIFT_TO = {"atlas_jsonl": ("tags", "nf.app", ("app", "service")),
            "prom": ("labels", "app", ("service",))}
_VAL = {"atlas_jsonl": "v", "prom": "value"}
_TAGS = {"atlas_jsonl": "tags", "prom": "labels"}


def _is_point(r) -> bool:
    return r.fmt == "atlas_jsonl" or (r.fmt == "prom" and r.fields.get("kind") == "sample")


def _key(r) -> str:
    tags = r.fields.get(_TAGS[r.fmt]) or {}
    return f"{r.fmt}:{r.fields['name']}{json.dumps(sorted(tags.items()))}"


def _series(self, recs, keep=lambda r: True) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for i, r in enumerate(recs):
        if self.eligible(r) and _is_point(r) and keep(r):
            out.setdefault(_key(r), []).append(i)
    return out


def _n_base(recs, idxs: list[int]) -> int:
    """How many of a series' records are not recovery records (all, normally)."""
    return sum(RECOVERY_TAG not in recs[i].tags for i in idxs)


def _from_baseline_pos(recs, idxs: list[int], pos) -> list[int]:
    """idxs (time-sorted) from the point `pos(n)` picks among the non-recovery
    ones on; just idxs[pos(len(idxs)):] when there are no recovery records."""
    base = [i for i in idxs if RECOVERY_TAG not in recs[i].tags]
    if len(base) == len(idxs):
        return idxs[pos(len(idxs)):]
    since = recs[base[pos(len(base))]].at_utc
    return [i for i in idxs if recs[i].at_utc >= since]


def _record_per_path(self, recs, idxs, series, **detail):
    paths: dict[str, int] = {}
    for i in idxs:
        paths[recs[i].rel_path] = paths.get(recs[i].rel_path, 0) + 1
    return [self.record(p, over=[recs[i] for i in idxs if recs[i].rel_path == p], series=series,
                        count=n, **detail) for p, n in paths.items()]


def _set_val(r, v):
    return replace(r, fields={**r.fields, _VAL[r.fmt]: v})


@injector("met.tag_key_drift", ("atlas", "prom"))
def tag_key_drift(self, recs, rng, scenario):
    """From mid-series on, the app tag key changes (nf.app -> app/service)."""
    ser = _series(self, recs, lambda r: DRIFT_TO[r.fmt][1] in (r.fields.get(_TAGS[r.fmt]) or {}))
    keys = base_keys(recs, ser)
    out, inj = list(recs), []
    for k in pick(rng, len(keys), DRIFT_RATE):
        idxs = sorted(ser[keys[k]], key=lambda i: recs[i].at_utc)
        tk, old, choices = DRIFT_TO[recs[idxs[0]].fmt]
        new = choices[int(rng.integers(len(choices)))]
        moved = _from_baseline_pos(recs, idxs, lambda n: n // 2)
        for i in moved:
            out[i] = replace(out[i], fields={**out[i].fields,
                                             tk: rename_key(out[i].fields[tk], old, new)})
        inj += _record_per_path(self, recs, moved, keys[k], from_key=old, to_key=new,
                                since=recs[moved[0]].at_utc)
    return out, inj


@injector("met.unit_seconds", ("atlas", "prom"))
def unit_seconds(self, recs, rng, scenario):
    """A latency series reported in seconds although its siblings use ms."""
    ser = _series(self, recs, lambda r: "latency" in r.fields["name"])
    keys = base_keys(recs, ser)
    out, inj = list(recs), []
    for k in pick(rng, len(keys), UNIT_RATE):
        for i in ser[keys[k]]:
            v = out[i].fields[_VAL[out[i].fmt]]
            if isinstance(v, (int, float)):
                out[i] = _set_val(out[i], v / 1000)
        inj += _record_per_path(self, recs, ser[keys[k]], keys[k], factor=0.001,
                                from_unit="ms", to_unit="s")
    return out, inj


@injector("met.counter_reset", ("prom",))
def counter_reset(self, recs, rng, scenario):
    """Process restart mid-series: cumulative counter restarts from zero."""
    ser = {k: v for k, v in _series(self, recs, lambda r: r.fields["name"].endswith("_total"))
           .items() if _n_base(recs, v) >= 2}
    keys = base_keys(recs, ser)
    out, inj = list(recs), []
    for k in pick(rng, len(keys), RESET_RATE):
        idxs = sorted(ser[keys[k]], key=lambda i: recs[i].at_utc)
        n_base = _n_base(recs, idxs)
        at = int(rng.integers(1, n_base))
        after = _from_baseline_pos(recs, idxs, lambda n: at)
        if n_base == len(idxs):
            offset = recs[idxs[at - 1]].fields["value"]
        else:  # the baseline record just before the reset
            offset = recs[[i for i in idxs if RECOVERY_TAG not in recs[i].tags
                           and recs[i].at_utc < recs[after[0]].at_utc][-1]].fields["value"]
        for i in after:
            out[i] = _set_val(out[i], out[i].fields["value"] - offset)
        inj += _record_per_path(self, recs, after, keys[k], offset=offset,
                                reset_at=recs[after[0]].at_utc)
    return out, inj


@injector("met.gap", ("atlas",))
def gap(self, recs, rng, scenario):
    """Collector outage: a run of points has no value (`v: null`). The points
    stay in place and the lost values are kept in the detail."""
    ser = {k: v for k, v in _series(self, recs).items() if _n_base(recs, v) >= 6}
    keys = base_keys(recs, ser)
    out, inj = list(recs), []
    for k in pick(rng, len(keys), GAP_RATE):
        idxs = sorted(ser[keys[k]], key=lambda i: recs[i].at_utc)
        n = int(rng.integers(3, min(10, len(idxs) // 2) + 1))
        s = int(rng.integers(0, len(idxs) - n + 1))
        run = idxs[s:s + n]
        values = [out[i].fields["v"] for i in run]
        for i in run:
            out[i] = _set_val(out[i], None)
        inj += _record_per_path(self, recs, run, keys[k], start=recs[run[0]].at_utc,
                                end=recs[run[-1]].at_utc, values=values)
    return out, inj


@injector("met.late", ("atlas",))
def late(self, recs, rng, scenario):
    """A run of points arrives late: appended after everything else in the file."""
    ser = {k: v for k, v in _series(self, recs).items() if _n_base(recs, v) >= 4}
    keys = base_keys(recs, ser)
    late_idx: list[int] = []
    inj = []
    for k in pick(rng, len(keys), LATE_RATE):
        idxs = sorted(ser[keys[k]], key=lambda i: recs[i].at_utc)
        s = int(rng.integers(0, len(idxs) - 3 + 1))
        run = idxs[s:s + 3]
        late_idx += run
        inj += _record_per_path(self, recs, run, keys[k], moved_to="end_of_file",
                                at_utc=[recs[i].at_utc for i in run])
    gone = set(late_idx)
    return [r for i, r in enumerate(recs) if i not in gone] + [recs[i] for i in late_idx], inj


@injector("met.duplicate_series", ("atlas", "prom"))
def duplicate_series(self, recs, rng, scenario):
    """A second collector re-emits a series. Atlas copies differ only in tag
    order; Prometheus copies are identical sample lines."""
    ser = _series(self, recs)
    keys = base_keys(recs, ser)
    after: dict[int, list] = {}
    inj = []
    for k in pick(rng, len(keys), DUP_RATE):
        for i in ser[keys[k]]:
            r = recs[i]
            if r.fmt == "atlas_jsonl":
                r = replace(r, fields={**r.fields,
                                       "tags": dict(reversed(list(r.fields["tags"].items())))})
            after[i] = [r]
        inj += _record_per_path(self, recs, ser[keys[k]], keys[k], placed="after_original")
    return with_inserts(recs, after), inj


INJECTORS = (tag_key_drift, unit_seconds, counter_reset, gap, late, duplicate_series)
