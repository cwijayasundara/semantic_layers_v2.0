"""A saved-run dict built from the correlation engine's output over the as-of store, so every
URI it names exists (the case-view tests and the API tests share it)."""
import copy
import json
from datetime import datetime, timedelta


def _call(tools, name: str, **args):
    return json.loads(tools[name].invoke(args))


def engine_run(tools, symptom: str) -> dict:
    """`root` = the engine's best chain's first link, `chain` = its links, `evidence` = the
    chain; every other change in the window is refuted "after onset"."""
    chains = _call(tools, "causal_chains", symptom_uri=symptom)
    chain = [link[3] for link in chains[0]["links"]]
    root = chain[0]
    found = _call(tools, "find_symptom", alert_id=symptom.rsplit("/", 1)[-1])
    alert = found["alert"]
    closure = _call(tools, "dependency_closure", app=alert["app"], direction="down")
    start = datetime.fromisoformat(alert["at"].replace("Z", "+00:00")) - timedelta(hours=6)
    changes = _call(tools, "changes_in_window", apps=[r["id"] for r in closure],
                    start=start.isoformat(), end=found["as_of"])
    refuted = [f"{c['uri']} refuted: after onset" for c in changes if c["uri"] != root]
    report = {"verdict": "root_cause_found", "summary": "engine-built test report",
              "hypotheses": [{"root_cause_uri": root, "chain": chain, "confidence": 0.64,
                              "evidence": list(chain), "refuted_alternatives": refuted}]}
    return {"report": report, "alert": alert["id"], "arm": "graph", "seconds": 1.0,
            "model_calls": 0, "tools": {"causal_chains": 1}}


def with_report(run: dict, **hypothesis) -> dict:
    """A deep copy of `run` with the top hypothesis' fields replaced."""
    out = copy.deepcopy(run)
    out["report"]["hypotheses"][0].update(hypothesis)
    return out
