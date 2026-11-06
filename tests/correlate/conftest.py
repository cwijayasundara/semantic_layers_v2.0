"""Correlation inputs drawn from the as-of stores the live RCA reads (R34): the hero's,
and the no-fault control's (generated, preprocessed and harvested at fixture scale)."""
import json

import pytest

FIXTURE_SCALE = 0.05      # the shared hero fixture's scale (tests/conftest.py)


@pytest.fixture(scope="session")
def harvested_hero_signals(harvested_hero_as_of):
    from e2ekg.graph.enrichers.correlation import chain_inputs
    return chain_inputs(harvested_hero_as_of.store)


@pytest.fixture(scope="session")
def hero_answers(harvested_hero_as_of) -> dict:
    return json.loads((harvested_hero_as_of.answers / "hero.json").read_text())


@pytest.fixture(scope="session")
def harvested_control_as_of(tmp_path_factory, scripted_llm):
    from e2ekg.generate.run import generate_scenario
    from e2ekg.graph.harvest import harvest, make_context
    from e2ekg.preprocess.asof import live_cutoff
    from e2ekg.preprocess.run import preprocess_scenario
    from e2ekg.scenario.variants import variant_scenarios
    base = tmp_path_factory.mktemp("control")
    raw, proc = base / "raw" / "v-control", base / "processed" / "v-control"
    generate_scenario(next(s for s in variant_scenarios() if s.name == "v-control"), raw,
                      scale=FIXTURE_SCALE)
    preprocess_scenario(raw, proc, llm=scripted_llm)
    out = base / "graph" / "v-control" / "as_of"
    ctx = make_context("v-control", proc, out, llm=scripted_llm, as_of=live_cutoff(proc).as_of)
    harvest(ctx, out=out, shacl=False)
    return ctx.store, base / "raw" / "_answers"


@pytest.fixture(scope="session")
def harvested_control_signals(harvested_control_as_of):
    from e2ekg.graph.enrichers.correlation import chain_inputs
    return chain_inputs(harvested_control_as_of[0])
