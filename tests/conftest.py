"""Shared pytest config. Helpers live in tests/helpers.py (importable as `helpers`).

Session fixtures: a scale-0.05 hero raw tree preprocessed once with the
scripted LLM (so the chat tables exist), and that tree harvested once into an
on-disk Oxigraph store, plus an as-of store of the same tree (R34); and the
no-fault control preprocessed the same way. The
generator builds the fixture (tests may use it); the pipeline under test never imports `e2ekg.generate` (R6). Tests may read
`_answers/` (the oracle), the pipeline may not (R5)."""
import pytest

from helpers import Harvested, Preprocessed

FIXTURE_SCALE = 0.05


@pytest.fixture(scope="session")
def scripted_llm():
    from e2ekg.scripted_llm import ScriptedClient
    return ScriptedClient()


@pytest.fixture(scope="session")
def preprocessed_hero(tmp_path_factory, scripted_llm) -> Preprocessed:
    from e2ekg.generate.run import generate_scenario
    from e2ekg.preprocess.run import preprocess_scenario
    from e2ekg.scenario.hero import hero_scenario
    base = tmp_path_factory.mktemp("pp")
    raw = base / "raw" / "hero"
    generate_scenario(hero_scenario(), raw, scale=FIXTURE_SCALE)
    proc = base / "processed" / "hero"
    report = preprocess_scenario(raw, proc, llm=scripted_llm)
    return Preprocessed(raw=raw, proc=proc, answers=base / "raw" / "_answers", report=report)


@pytest.fixture(scope="session")
def harvested_hero(tmp_path_factory, preprocessed_hero, scripted_llm) -> Harvested:
    from e2ekg.graph.harvest import harvest, make_context
    out = tmp_path_factory.mktemp("graph") / "hero"
    ctx = make_context("hero", preprocessed_hero.proc, out, llm=scripted_llm)
    report = harvest(ctx, out=out)
    return Harvested(store=ctx.store, report=report, proc=preprocessed_hero.proc,
                     answers=preprocessed_hero.answers, out=out, symptom=ctx.symptom)


@pytest.fixture(scope="session")
def harvested_hero_as_of(tmp_path_factory, preprocessed_hero, scripted_llm) -> Harvested:
    """The same tree harvested as of the live incident's cutoff (R34)."""
    from e2ekg.graph.harvest import harvest, make_context
    from e2ekg.preprocess.asof import live_cutoff
    out = tmp_path_factory.mktemp("graph") / "hero" / "as_of"
    as_of = live_cutoff(preprocessed_hero.proc).as_of
    ctx = make_context("hero", preprocessed_hero.proc, out, llm=scripted_llm, as_of=as_of)
    report = harvest(ctx, out=out)
    return Harvested(store=ctx.store, report=report, proc=preprocessed_hero.proc,
                     answers=preprocessed_hero.answers, out=out, as_of=as_of, symptom=ctx.symptom)


@pytest.fixture(scope="session")
def preprocessed_control(tmp_path_factory, scripted_llm) -> Preprocessed:
    """The no-fault control variant at fixture scale: no incident opens in its window."""
    from e2ekg.generate.run import generate_scenario
    from e2ekg.preprocess.run import preprocess_scenario
    from e2ekg.scenario.variants import variant_scenarios
    base = tmp_path_factory.mktemp("control")
    raw, proc = base / "raw" / "v-control", base / "processed" / "v-control"
    generate_scenario(next(s for s in variant_scenarios() if s.name == "v-control"), raw,
                      scale=FIXTURE_SCALE)
    report = preprocess_scenario(raw, proc, llm=scripted_llm)
    return Preprocessed(raw=raw, proc=proc, answers=base / "raw" / "_answers", report=report)


@pytest.fixture(scope="session")
def harvested_hero_store(harvested_hero):
    return harvested_hero.store
