"""The agent tools over the hero's as-of store (R34), on a private in-memory copy so the
findings a test records never reach the session fixtures other tests share."""
import json
from types import SimpleNamespace

import pytest

from e2ekg.agent.tools import make_tools
from e2ekg.graph.store import Store


def copy_store(src: Store) -> Store:
    dst = Store(None)
    dst.oxigraph.extend(src.oxigraph)
    return dst


@pytest.fixture(scope="module")
def live(harvested_hero_as_of, preprocessed_hero, scripted_llm):
    h = harvested_hero_as_of
    store = copy_store(h.store)
    tools = make_tools(store, preprocessed_hero.proc, "hero", "tester", llm=scripted_llm)
    answers = json.loads((h.answers / "hero.json").read_text())
    return SimpleNamespace(store=store, tools=tools, as_of=h.as_of, proc=preprocessed_hero.proc,
                           raw=preprocessed_hero.raw, answers=answers, symptom=h.symptom,
                           llm=scripted_llm)


@pytest.fixture(scope="module")
def tools(live):
    return live.tools


@pytest.fixture(scope="module")
def full(harvested_hero, harvested_hero_as_of, preprocessed_hero, scripted_llm):
    """The same tools over the FULL store, with the live cutoff: what a leak would look like."""
    return make_tools(harvested_hero.store, preprocessed_hero.proc, "hero", "leaky",
                      llm=scripted_llm, as_of=harvested_hero_as_of.as_of)


def call(tools, name: str, **args):
    return json.loads(tools[name].invoke(args))
