"""The raw_only arm (R53): the AutoRCA commander's job, done by one DeepAgents agent over the
raw files as they were at the cutoff, with no graph, no graph tools and no specialists. It is
the baseline for graph_no_evidence_tool, the graph agent without its evidence tool: both have
only their own reading of the telemetry to weigh, one over a knowledge graph and one over
files. The agent sees the as-of raw view (`rawview`), read-only, mounted at /raw/; its scratch
space is agent state, and it is the same harness profile (`harnessed`) the graph arms use."""
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from deepagents.backends.protocol import DeleteResult, EditResult, FileUploadResponse, WriteResult
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphRecursionError

from e2ekg.agent.autorca import RECURSION_LIMIT, failed, harnessed
from e2ekg.agent.report import RCAReport
from e2ekg.agent.specialists import prompt
from e2ekg.llm import MODEL_ID

MOUNT = "/raw/"
_READ_ONLY = "the raw files are read-only"


class ReadOnlyFilesystem(FilesystemBackend):
    """A FilesystemBackend that refuses every write, so one run cannot change what the next
    run reads."""

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=_READ_ONLY)

    def edit(self, file_path: str, old_string: str, new_string: str,
             replace_all: bool = False) -> EditResult:
        return EditResult(error=_READ_ONLY)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=_READ_ONLY)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=p, error="permission_denied") for p, _ in files]


def build_raw_only(raw_view: Path, model: str | BaseChatModel = f"fireworks:{MODEL_ID}"):
    return create_deep_agent(
        model=harnessed(model), tools=[], system_prompt=prompt("raw_only"),
        response_format=ToolStrategy(RCAReport), name="raw-only",
        backend=CompositeBackend(default=StateBackend(), routes={
            MOUNT: ReadOnlyFilesystem(root_dir=Path(raw_view), virtual_mode=True)}))


def run_raw_only(alert_id: str, raw_view: Path, *,
                 model: str | BaseChatModel = f"fireworks:{MODEL_ID}",
                 callbacks: list | None = None) -> RCAReport:
    agent = build_raw_only(raw_view, model)
    try:
        out = agent.invoke(
            {"messages": [HumanMessage(f"Alert {alert_id} fired. Find its root cause and "
                                       "return the RCAReport.")]},
            config={"recursion_limit": RECURSION_LIMIT, "callbacks": callbacks or []})
    except GraphRecursionError:
        return failed(f"GraphRecursionError: no report within {RECURSION_LIMIT} steps")
    report = out.get("structured_response")
    return report if report is not None else failed(
        "no structured response: the agent stopped without returning an RCAReport")
