"""The ranking survives perturbations a real incident would bring (R41, R42): every red
herring moved within a minute of where it landed, in every fault scenario, on the
fixture as-of stores. The hero runs by default; the variants are slow (each builds its
own fixture, about 30 s)."""
from dataclasses import replace
from datetime import timedelta

import pytest

from e2ekg.correlate.engine import correlate
from e2ekg.graph.uris import change_uri
from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.variants import variant_scenarios

SCENARIOS = {s.name: s for s in (hero_scenario(), *variant_scenarios()) if s.name != "v-control"}
SHIFTS_S = (-60, -30, -1, 30)


def _cases():
    for name, s in SCENARIOS.items():
        for h in s.answer_key.red_herring_change_ids:
            for dt in SHIFTS_S:
                marks = [] if name == "hero" else [pytest.mark.slow]
                yield pytest.param(name, h, dt, marks=marks, id=f"{name}-{h}-{dt:+d}s")


@pytest.mark.parametrize(("name", "herring", "dt"), list(_cases()))
def test_a_red_herring_moved_within_a_minute_stays_out_of_the_top_three(
        fault_inputs, name, herring, dt):
    (signals, edges, symptom), answers = fault_inputs(name)
    s = SCENARIOS[name]
    kind = {c.id: c.kind for c in s.changes}
    external = {v: k for k, v in answers["external_ids"].items()}

    def uri(cid):
        return str(change_uri(name, kind[cid], external[cid]))
    target = next(x for x in signals if x.uri == uri(herring))
    moved = [replace(x, at_utc=x.at_utc + timedelta(seconds=dt)) if x == target else x
             for x in signals]
    roots = [c.links[0].uri for c in correlate(moved, edges, symptom)]
    assert roots[:1] == [uri(s.answer_key.root_cause_change_id)]
    assert uri(herring) not in roots[:3]
