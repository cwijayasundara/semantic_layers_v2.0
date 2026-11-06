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
