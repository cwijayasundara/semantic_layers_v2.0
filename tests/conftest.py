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
def preprocessed(tmp_path_factory, scripted_llm, preprocessed_hero, preprocessed_control):
    """name -> Preprocessed for the hero, the control and every fault variant, each built once
    (variants at fixture scale)."""
    from e2ekg.generate.run import generate_scenario
    from e2ekg.preprocess.run import preprocess_scenario
    from e2ekg.scenario.variants import variant_scenarios
    built = {"hero": preprocessed_hero, "v-control": preprocessed_control}

    def get(name: str) -> Preprocessed:
        if name not in built:
            base = tmp_path_factory.mktemp(name)
            raw, proc = base / "raw" / name, base / "processed" / name
            generate_scenario(next(s for s in variant_scenarios() if s.name == name), raw,
                              scale=FIXTURE_SCALE)
            built[name] = Preprocessed(raw=raw, proc=proc, answers=base / "raw" / "_answers",
                                       report=preprocess_scenario(raw, proc, llm=scripted_llm))
        return built[name]
    return get


@pytest.fixture(scope="session")
def harvested(tmp_path_factory, scripted_llm, preprocessed, harvested_hero, harvested_hero_as_of):
    """(name, as_of=False) -> Harvested: the scenario's full store, or the as-of store the live
    RCA reads (R34). One harvest per store, SHACL on, shared by every test."""
    from e2ekg.graph.harvest import harvest, make_context
    from e2ekg.preprocess.asof import live_cutoff
    built = {("hero", False): harvested_hero, ("hero", True): harvested_hero_as_of}

    def get(name: str, as_of: bool = False) -> Harvested:
        if (name, as_of) not in built:
            pp = preprocessed(name)
            out = tmp_path_factory.mktemp("graph") / name / ("as_of" if as_of else "full")
            cut = live_cutoff(pp.proc).as_of if as_of else None
            ctx = make_context(name, pp.proc, out, llm=scripted_llm, as_of=cut)
            report = harvest(ctx, out=out)
            built[(name, as_of)] = Harvested(store=ctx.store, report=report, proc=pp.proc,
                                             answers=pp.answers, out=out, as_of=cut,
                                             symptom=ctx.symptom)
        return built[(name, as_of)]
    return get


@pytest.fixture(scope="session")
def harvested_hero_store(harvested_hero):
    return harvested_hero.store


@pytest.fixture(scope="session")
def derive_module():
    import importlib.util

    from e2ekg.graph.shacl import CQ_DIR
    spec = importlib.util.spec_from_file_location("_derive_expected", CQ_DIR / "_derive_expected.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def fixture_expected(harvested_hero, derive_module, tmp_path_factory):
    """The CQs re-derived from the 0.05 fixture's own generation (R25), as
    `<stem>.rq` + `<stem>.expected.json` pairs in tmp."""
    import json

    from e2ekg.graph.shacl import CQ_DIR
    d = tmp_path_factory.mktemp("cq_fixture")
    for stem, payload in derive_module.derive(answers=harvested_hero.answers).items():
        (d / f"{stem}.rq").write_text((CQ_DIR / f"{stem}.rq").read_text())
        (d / f"{stem}.expected.json").write_text(json.dumps(payload, indent=2) + "\n")
    return d
