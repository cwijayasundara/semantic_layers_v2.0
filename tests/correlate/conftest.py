"""Correlation inputs drawn from the as-of stores the live RCA reads (R34): the hero's,
and the no-fault control's (harvested at fixture scale)."""
import json

import pytest

from helpers import Harvested


@pytest.fixture(scope="session")
def harvested_hero_signals(harvested_hero_as_of):
    from e2ekg.graph.enrichers.correlation import chain_inputs
    return chain_inputs(harvested_hero_as_of.store, harvested_hero_as_of.symptom)


@pytest.fixture(scope="session")
def hero_answers(harvested_hero_as_of) -> dict:
    return json.loads((harvested_hero_as_of.answers / "hero.json").read_text())


@pytest.fixture(scope="session")
def harvested_control_as_of(harvested) -> Harvested:
    return harvested("v-control", as_of=True)


@pytest.fixture(scope="session")
def harvested_control_signals(harvested_control_as_of):
    from e2ekg.graph.enrichers.correlation import chain_inputs
    return chain_inputs(harvested_control_as_of.store, harvested_control_as_of.symptom)


@pytest.fixture(scope="session")
def fault_inputs(harvested):
    """name -> (correlate inputs over its fixture as-of store, its answers JSON), built once
    per scenario."""
    from e2ekg.graph.enrichers.correlation import chain_inputs
    built = {}

    def get(name: str):
        if name not in built:
            h = harvested(name, as_of=True)
            answers = json.loads((h.answers / f"{name}.json").read_text())
            built[name] = chain_inputs(h.store, h.symptom), answers
        return built[name]
    return get
