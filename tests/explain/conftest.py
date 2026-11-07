"""The agent tools over the hero's as-of store, on a private in-memory copy (as tests/agent)."""
from types import SimpleNamespace

import pytest

from agent_helpers import copy_store

from e2ekg.agent.tools import make_tools


@pytest.fixture(scope="module")
def live_tools(harvested_hero_as_of, preprocessed_hero, scripted_llm):
    h = harvested_hero_as_of
    store = copy_store(h.store)
    tools = make_tools(store, preprocessed_hero.proc, "hero", "explain", llm=scripted_llm)
    return SimpleNamespace(store=store, tools=tools, proc=preprocessed_hero.proc,
                           as_of=h.as_of, symptom=h.symptom, llm=scripted_llm)
