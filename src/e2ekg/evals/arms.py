"""The names of the eval arms (R53), light enough for the command line to import.

`graph_fast` (the fast path, e2ekg.agent.fastpath) is opt-in: `all` keeps the Deep Path arms
the evals have always measured (A.2 — do not silently swap what the evals measure), so it is
scored only when named explicitly."""
LLM_ARMS = ("graph", "graph_no_evidence_tool", "raw_only")
FAST_ARM = "graph_fast"
ENGINE_ARMS = ("engine_top", "engine_no_topology", "engine_no_explain_away")
ARMS = (*LLM_ARMS, FAST_ARM, *ENGINE_ARMS)
DEFAULT_ARMS = (*LLM_ARMS, *ENGINE_ARMS)
