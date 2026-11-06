import json
import re
from collections.abc import Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from e2ekg.graph.store import Store


def copy_store(src: Store) -> Store:
    dst = Store(None)
    dst.oxigraph.extend(src.oxigraph)
    return dst


def call(tools, name: str, **args):
    return json.loads(tools[name].invoke(args))


class FakeToolModel(BaseChatModel):
    """A chat model that answers by `policy(messages, bound_tool_names) -> AIMessage`, so one
    instance can play the commander and every specialist: each agent binds its own tools."""
    policy: Callable[[list[BaseMessage], frozenset[str]], AIMessage]
    model_name: str = "fake-tool-model"
    bound: frozenset[str] = frozenset()

    def bind_tools(self, tools, **kwargs):
        names = frozenset(t.name if hasattr(t, "name") else t.get("name") or t["function"]["name"]
                          for t in tools)
        return self.model_copy(update={"bound": names})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.policy(messages, self.bound))])

    @property
    def _llm_type(self) -> str:
        return "fake-tool-model"


def fake_tool_model(policy=None) -> FakeToolModel:
    return FakeToolModel(policy=policy or (lambda messages, bound: AIMessage("nothing to do")))


def subagent_names(agent) -> set[str]:
    """The subagent types the commander's task tool offers."""
    task = agent.nodes["tools"].bound.tools_by_name["task"]
    return set(re.findall(r"^- ([\w-]+): ", task.description, re.M))


def tool_names(sub) -> set[str]:
    return {t.name for t in sub["tools"]}
