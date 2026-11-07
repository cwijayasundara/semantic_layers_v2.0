"""The names of the eval arms (R53), light enough for the command line to import."""
LLM_ARMS = ("graph", "graph_no_evidence_tool", "raw_only")
ENGINE_ARMS = ("engine_top", "engine_no_topology", "engine_no_explain_away")
ARMS = (*LLM_ARMS, *ENGINE_ARMS)
