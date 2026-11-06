"""Session fixture: a scale-0.05 hero raw tree, preprocessed once.

The generator builds the fixture (tests may use it); the pipeline under test
never imports `e2ekg.generate` (R6). Tests may read `_answers/` (the oracle),
the pipeline may not (R5)."""
from dataclasses import dataclass
from pathlib import Path

import pytest

FIXTURE_SCALE = 0.05


@dataclass(frozen=True)
class Preprocessed:
    raw: Path        # <tmp>/raw/hero
    proc: Path       # <tmp>/processed/hero
    answers: Path    # <tmp>/raw/_answers
    report: object   # PreprocessReport


@pytest.fixture(scope="session")
def preprocessed_hero(tmp_path_factory) -> Preprocessed:
    from e2ekg.generate.run import generate_scenario
    from e2ekg.preprocess.run import preprocess_scenario
    from e2ekg.scenario.hero import hero_scenario
    base = tmp_path_factory.mktemp("pp")
    raw = base / "raw" / "hero"
    generate_scenario(hero_scenario(), raw, scale=FIXTURE_SCALE)
    proc = base / "processed" / "hero"
    report = preprocess_scenario(raw, proc, llm=None)
    return Preprocessed(raw=raw, proc=proc, answers=base / "raw" / "_answers", report=report)

