"""The investigation trail: what the agent called and what it concluded.

    trail = Trail()
    run_rca(alert, scenario, trail=trail)
    trail.dump()    # {"steps": [{i, at, tool, args, output}], "findings": [{uri, ...}]}

`Trail` is a LangChain callback handler: passed through the run config it is inherited by the
specialist sub-agents, so their tool calls land in the same log. Findings are read from the
agent's private store copy, which is discarded when the run ends."""
import json
import threading
from datetime import UTC, datetime
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from e2ekg.graph.store import Store
from e2ekg.graph.uris import BASE, NS
from e2ekg.preprocess.identity import APP_URI_PREFIX

OUTPUT_CHARS = 2000


class Trail(BaseCallbackHandler):
    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.findings: list[dict] = []
        self._by_run: dict[UUID, dict] = {}
        self._lock = threading.Lock()

    def on_tool_start(self, serialized, input_str, *, run_id, inputs=None, **kwargs):
        args = inputs if isinstance(inputs, dict) else _as_dict(input_str)
        with self._lock:
            step = {"i": len(self.steps), "at": datetime.now(UTC).isoformat(),
                    "tool": (serialized or {}).get("name") or kwargs.get("name") or "?",
                    "args": args, "output": ""}
            self.steps.append(step)
            self._by_run[run_id] = step

    def on_tool_end(self, output, *, run_id, **kwargs):
        self._finish(run_id, getattr(output, "content", output))

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._finish(run_id, f"error: {error}")

    def note(self, tool: str, args: dict, output) -> None:
        """A step the agent loop did not make: a fast-path prefetch, specialist call or
        escalation (see e2ekg.agent.fastpath)."""
        with self._lock:
            self.steps.append({"i": len(self.steps), "at": datetime.now(UTC).isoformat(),
                               "tool": tool, "args": args, "output": str(output)[:OUTPUT_CHARS]})

    def _finish(self, run_id: UUID, output) -> None:
        text = output if isinstance(output, str) else json.dumps(output, default=str)
        with self._lock:
            step = self._by_run.pop(run_id, None)
            if step is not None:
                step["output"] = text[:OUTPUT_CHARS]

    def dump(self) -> dict:
        return {"steps": self.steps, "findings": self.findings}


def _as_dict(text) -> dict:
    try:
        v = json.loads(text) if isinstance(text, str) else text
    except ValueError:
        return {"input": text}
    return v if isinstance(v, dict) else {"input": v}


def findings_of(store: Store, scenario: str) -> list[dict]:
    """Every ops:Finding the specialists recorded for `scenario`, sorted by uri."""
    prefix = f"{BASE}/graph/findings/{scenario}/"
    rows = store.query(f"""PREFIX ops: <{NS["ops"]}> PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT ?f ?g ?app ?claim ?conf
               (GROUP_CONCAT(DISTINCT STR(?cite); SEPARATOR=" ") AS ?cites) WHERE {{
        GRAPH ?g {{ ?f a ops:Finding . OPTIONAL {{ ?f ops:onApplication ?app }}
                    OPTIONAL {{ ?f rdfs:comment ?claim }} OPTIONAL {{ ?f ops:confidence ?conf }}
                    OPTIONAL {{ ?f ops:cites ?cite }} }}
        FILTER(STRSTARTS(STR(?g), "{prefix}")) }}
        GROUP BY ?f ?g ?app ?claim ?conf ORDER BY ?f""")
    return [{"uri": r["f"], "specialist": r["g"][len(prefix):],
             "app": _app(r["app"]),
             "claim": r["claim"], "confidence": float(r["conf"]) if r["conf"] else None,
             "cites": sorted((r["cites"] or "").split())} for r in rows]


def _app(u: str | None) -> str | None:
    return u[len(APP_URI_PREFIX):] if u and u.startswith(APP_URI_PREFIX) else u
