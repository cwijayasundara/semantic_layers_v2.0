"""Correlation inputs drawn from the as-of stores the live RCA reads (R34): the hero's,
and the no-fault control's (harvested at fixture scale)."""
import json

import pytest

from helpers import Harvested

FIXTURE_SCALE = 0.05      # the shared hero fixture's scale (tests/conftest.py)


@pytest.fixture(scope="session")
def harvested_hero_signals(harvested_hero_as_of):
    from e2ekg.graph.enrichers.correlation import chain_inputs
    return chain_inputs(harvested_hero_as_of.store, harvested_hero_as_of.symptom)


@pytest.fixture(scope="session")
def hero_answers(harvested_hero_as_of) -> dict:
    return json.loads((harvested_hero_as_of.answers / "hero.json").read_text())


@pytest.fixture(scope="session")
def harvested_control_as_of(tmp_path_factory, preprocessed_control, scripted_llm) -> Harvested:
    from e2ekg.graph.harvest import harvest, make_context
    from e2ekg.preprocess.asof import live_cutoff
    out = tmp_path_factory.mktemp("graph") / "v-control" / "as_of"
    as_of = live_cutoff(preprocessed_control.proc).as_of
    ctx = make_context("v-control", preprocessed_control.proc, out, llm=scripted_llm, as_of=as_of)
    report = harvest(ctx, out=out, shacl=False)
    return Harvested(store=ctx.store, report=report, proc=preprocessed_control.proc,
                     answers=preprocessed_control.answers, out=out, as_of=as_of,
                     symptom=ctx.symptom)


@pytest.fixture(scope="session")
def harvested_control_signals(harvested_control_as_of):
    from e2ekg.graph.enrichers.correlation import chain_inputs
    return chain_inputs(harvested_control_as_of.store, harvested_control_as_of.symptom)


@pytest.fixture(scope="session")
def fault_inputs(tmp_path_factory, scripted_llm, harvested_hero_as_of):
    """name -> (correlate inputs over its fixture as-of store, its answers JSON), built once
    per scenario: the hero's shared fixture, or a variant generated at fixture scale."""
    from e2ekg.graph.enrichers.correlation import chain_inputs
    built = {}

    def get(name: str):
        if name not in built:
            h = harvested_hero_as_of if name == "hero" else _variant_as_of(
                tmp_path_factory, scripted_llm, name)
            answers = json.loads((h.answers / f"{name}.json").read_text())
            built[name] = chain_inputs(h.store, h.symptom), answers
        return built[name]
    return get


def _variant_as_of(tmp_path_factory, llm, name: str) -> Harvested:
    from e2ekg.generate.run import generate_scenario
    from e2ekg.graph.harvest import harvest, make_context
    from e2ekg.preprocess.asof import live_cutoff
    from e2ekg.preprocess.run import preprocess_scenario
    from e2ekg.scenario.variants import variant_scenarios
    base = tmp_path_factory.mktemp(name)
    raw, proc, out = base / "raw" / name, base / "processed" / name, base / "graph" / name
    generate_scenario(next(s for s in variant_scenarios() if s.name == name), raw,
                      scale=FIXTURE_SCALE)
    preprocess_scenario(raw, proc, llm=llm)
    as_of = live_cutoff(proc).as_of
    ctx = make_context(name, proc, out, llm=llm, as_of=as_of)
    report = harvest(ctx, out=out, shacl=False)
    return Harvested(store=ctx.store, report=report, proc=proc, answers=base / "raw" / "_answers",
                     out=out, as_of=as_of, symptom=ctx.symptom)
