"""Log selectors of the as-of store carry as-of template ids (mined from the lines observable
at the cutoff); the agent's raw_evidence resolves them against those same lines."""
import json

from agent_helpers import call
from helpers import asof_only_pattern

from e2ekg.graph.enricher import node
from e2ekg.preprocess import selector as sel


def test_asof_log_selector_resolves(live):
    app, tid, template, ids = asof_only_pattern(live.proc, live.as_of)
    uri = str(node("log", "pattern", "hero", app, tid))
    rows = call(live.tools, "sparql_select",
                query=f"SELECT ?t ?s WHERE {{ <{uri}> log:template ?t ; ops:rawSelector ?s }}")
    assert rows and rows[0]["t"] == template
    selector = sel.Selector.from_json(rows[0]["s"])
    assert (selector.app_uri, selector.key) == (app, tid)
    for target in (uri, rows[0]["s"]):
        got = json.loads(live.tools["raw_evidence"].invoke({"uri_or_selector": target,
                                                            "limit": 100}))
        assert [r["raw_id"] for r in got] == ids[:100]
        assert all("line" in r for r in got)
