# Proposal — Fast AutoRCA (<10s) and a Navigable Evidence Graph

Two asks from the live demo (AutoRCA on the `v-bad-deploy` alert
*"TTR p90 SLO burn rate 14.4x on edge-gateway"*): the run took **137 s** — bring it
under **10 s** — and the report's evidence is a wall of URIs; make it a **navigable
graph** that *convinces an audience* this is the right RCA, not just any RCA.

This proposal is grounded in a measured run: `data/evals/recovery-1run/runs/v-bad-deploy__graph__0.json`
(same alert, graph arm: **124.6 s**; the live job's extra ~12 s is Fireworks latency
variance and cold Parquet pages).

---

## Part A — Where the 137 seconds actually go

```
 timeline of one graph-arm run (124.6 s total, 43 LLM calls, 410K in / 38.6K out tokens)

 0s      find_symptom
 0– 9s   commander think #1                        ~9s  (cold start + big prompt)
 9–63s   7 specialists, run SEQUENTIALLY          ~55s  <-- the structural problem
         (each: 3–5 LLM turns x ~4–6s + tool time;
          steps 1–7 are all `task`, one after another)
 63–68s  commander read_findings                    5s
 68–81s  commander turns: sparql, chains, changes  13s  (think gaps 8.3s, 10.1s)
 81–101s commander raw_evidence drill-down         20s  (20 calls, 2–3.5s each)
 101–125s final RCAReport (ToolStrategy)           ~24s  (full context re-sent)
```

Diagnosis, in order of impact:

| # | Problem | Evidence | Cost |
|---|---------|----------|------|
| 1 | **Specialists run sequentially.** The model emits all 7 `task` calls in one turn (steps 1–7 at 9.15–9.27 s) but DeepAgents/LangGraph executes subagent tool calls one after another | trail steps 8–60 interleave in a single stream | ~55 s → could be ~10 s wall |
| 2 | **Commander needs too many turns.** find_symptom → dispatch → read_findings → sparql/chains → changes → candidate_evidence → raw_evidence ×N → report; each think gap grows with context | gaps of 9.2, 8.3, 10.1, 5.2, 3.9 s | ~50 s |
| 3 | **Context bloat.** 410K input tokens / 43 calls ≈ 9.5K avg, but late commander calls re-send every prior tool result; the final report call re-sends everything (~24 s) | token totals; report phase | ~20–25 s |
| 4 | **raw_evidence is 20 calls**, the most-invoked tool, at 2–3.5 s each (DuckDB over Parquet + raw byte reads) | 20 of 82 steps | ~15 s (much overlapped with #2) |
| 5 | Specialist boilerplate: each burns 1–2 turns just calling its slice tools before it could know enough to find anything | 29 record_finding + ~25 slice calls | folded into #1 |

### A.1 Phase 1 — Reorder, prefetch, trim (no architecture change; ~137s → ~25–30s)

1. **Dispatch specialists concurrently.** Take dispatch out of the agent loop: the
   orchestrator (`autorca.py`) runs the 7 specialist agents itself with
   `asyncio.gather` (each is an independent `create_deep_agent` on its own tool
   slice). The commander no longer gets the `task` tool for specialists; it gets
   their findings. Wall time for the specialist phase = slowest specialist (~10–12 s).
   - Keep `record_finding` exactly as is — findings still land as `ops:Finding`
     triples in each specialist's named graph; the RDF interface is unchanged.
   - Sort trail steps deterministically (by specialist, then `i`) so runs stay comparable.
2. **Prefetch the commander's first two turns.** `find_symptom`, `causal_chains` and
   `changes_in_window` are deterministic queries — run them before `invoke` and put
   the JSON in the opening human message. Commander starts at "read findings".
   Saves 2 round-trips (~15 s).
3. **Batch + cap raw_evidence.** One call may cite many URIs/selectors (it already
   accepts lists); prompt the commander: ≤2 raw_evidence calls before reporting, and
   have the tool return digests with `raw_id` pointers rather than full lines — the
   UI drills into bytes on click, not the agent. Saves ~10–15 s.
4. **Trim what read_findings returns** (claim digests, cites capped) and what a
   specialist's final message may contain (≤ 500 tokens). Directly shrinks the
   report call's prefill (#3).

*Expected: ~25–30 s. This alone makes the demo feel live. The next phase gets <10 s.*

### A.2 Phase 2 — The Fast Path (<10 s) — **implemented, decode-bound at ~17–20 s**

> **Status (measured):** `agent/fastpath.py` + the opt-in `graph_fast` eval arm ship
> the structure below — 43 → 8 model calls, 410K → 33K input tokens, ~32.5 s →
> **~17–20 s** on the hero alert (prefetch 1 s, 7 specialists in parallel 4 s,
> synthesis ~12 s), correct root at confidence 0.9, no escalation, eval top-1.
> The <10 s estimate assumed ~4 s per decode phase; GLM-5.3-flash decodes at
> ~60 tok/s even with `reasoning_effort: low`, so the wall is two decode phases,
> not call structure. The §A.3 contingencies are the path below 10 s. Output
> caps (≤2 findings per specialist, ≤3-sentence summary) bought 20.4 → 17.1 s.

The insight: **by the time a specialist's tool loop ends, everything it learned was
computable deterministically in advance.** The LLM's irreplaceable job is judgment,
not fetching. So:

```
  t=0.0s  PREFETCH (pure SPARQL/DuckDB, <0.5s, deterministic)
          symptom, engine chains, changes_in_window, and each specialist's
          full tool slice precomputed with the SAME tools it would have called
  t=0.5s  7 SPECIALISTS IN PARALLEL (asyncio.gather)
          each = 1 LLM call: slice JSON in the prompt -> FindingIn[] as
          structured output (no tool loop, no record_finding round-trip;
          orchestrator writes the findings triples afterwards)
          wall = slowest specialist ........................... ~4s
  t=4.5s  COMMANDER SYNTHESIS = 1 LLM call
          prefetched candidate_evidence facts + findings digest + engine chains
          -> RCAReport (ToolStrategy) ......................... ~4s
  t=9s    done
```

- Total LLM calls: 8–9 (was 43). Total input tokens: ~60–80K (was 410K).
- **Fireworks prompt caching** rewards the fixed prefixes (system prompts, tool
  schemas) across the 7 parallel specialist calls and across runs.
- **Escalation, not regression:** if the commander's confidence is low or hypotheses
  conflict, it *escalates to the Deep Path* — the full Phase-1 agentic run — so the
  fast path never bounds investigation quality, only the happy path.
- **Evals stay honest:** `evals/` keeps running the Deep Path arms unchanged. Add an
  optional `graph_fast` arm later to measure fast-path accuracy separately; do not
  silently swap what the evals measure.

### A.3 Phase 2.5 — If flash isn't fast enough (contingencies)

Status against the measured 17–20 s: the specialist model is already the small
half of the wall (~4 s); the synthesis decode (~12 s) is the lever that matters
and none of the levers below shrink it without giving up the judgment — the
corroboration shortcut (first row) is the only one that removes calls entirely.

| Lever | Effect | Trade-off |
|---|---|---|
| Skip specialists when engine top-chain confidence ≥ 0.8 **and** `candidate_evidence` corroborates it | 2–3 LLM calls total, ~6–8 s | specialists' independent perspectives lost on easy cases — acceptable: the corroboration check is exactly what they'd add |
| Smaller/faster model for specialists (commander stays on GLM-5.3-flash) | −1–2 s per call | findings quality drops first — measure with the eval harness before shipping |
| Stream the report tokens | perceived latency ≈ 0 | report renders as it arrives |

### A.4 What we are *not* doing

- Not removing the as-of store, the tool caps, or the cutoff enforcement — latency
  fixes must not touch correctness invariants (R34, R50, R53).
- Not making specialists "smarter" with more context — the opposite; prefetch makes
  them cheaper *and* faster.
- Not caching reports across runs: same alert + same as-of store → same report is
  the reproducibility property, but the demo value is watching it reason.

---

## Part B — Evidence as a navigable graph (not a URI dump)

### B.1 The problem, precisely

The report's hypotheses carry `chain: list[str]` and `evidence: list[str]` — RDF
URIs. The UI renders them as text (the wall you pasted). The information density is
fine; the *affordance* is wrong. An audience member cannot see: what is a change vs
a signal, which URI caused which, why the others were rejected, or that each URI
sits on raw bytes you can read. The RDF is already there — `ops:cites`,
`prov:wasDerivedFrom`, `ops:rawSelector`, `ops:dependsOn`, `ops:atTime` — we just
render its projection as flat text.

### B.2 What already exists (we build on all of it)

- `explain/case.py` — `build_case` assembles `{meta, nodes, edges, trail}` from the
  as-of store through the agent's own tools; `facts` (from `candidate_evidence`) and
  `agent_reason` (from `refuted_alternatives`) are already separate fields.
- `ui/components/case/CaseGraph.tsx` — Cytoscape + fcose already rendering
  typed/categorized nodes with layer toggles; `swimlane.ts` time axis; SidePanel,
  AfterPanel, TrailPanel, Scorecard.
- `GET /api/scenarios/{s}/case` and the `/after`, `/lineage`, `raw_evidence` machinery.

So the gap is narrow: **the hypothesis → evidence argument is not a first-class
subgraph**, and URIs render as URIs instead of as nodes you can walk.

### B.3 The design — "Argument view"

```
                    ROOT-CAUSE ARGUMENT (one hypothesis = one layered subgraph)

   SYMPTOM      ┌────────────────────────────┐
   (top)        │  alrt/alert radar-09000    │  TTR p90 burn 14.4x, 15:30, edge-gateway
                └─────────────▲──────────────┘
   SIGNALS            cites    │  each edge = a claim with provenance
   (middle,           ┌────────┴────────┬──────────────┬─────────────┐
   time-ordered)      │ met/anomaly     │ log/spike    │ trc/finger… │   rows call-path
                ┌─────▲─────────┐ ┌─────▲───────┐ ┌────▲──────┐     │    badges
   ROOT         │ chg/config_change: min 48 → 12 @ 14:58 (root)         │   degraded /
   (bottom)     └───────────────────────────────────────────────────────┘   not_degraded /
                                                                             unknown
   SIDE PANEL   Refuted alternatives: android client release (+8 min — but nothing it
                (right)               calls was degraded: topology gate)
                                     api-gateway deploy (prior from history — but cascade
                                     had already begun: explaining away) …

   every node click → SidePanel: rdf:type, ops:atTime, confidence,
   ops:rawSelector → [show raw lines] → the actual bytes (raw_evidence)
```

**Data:** extend `build_case` with one new block, `hypotheses[]` — for each report
hypothesis, its RDF neighborhood: nodes (label, kind, `ops:atTime`, severity,
confidence) and edges (`ops:cites`, `prov:wasDerivedFrom`, `ops:dependsOn`,
chat `mentions`), plus each refuted alternative paired with its parsed refuting fact
(`explain/refuted.py` already parses these). On-demand expansion endpoint for
navigation:

```
GET /api/scenarios/{s}/case                      (existing, + hypotheses[])
GET /api/scenarios/{s}/case/expand?uri=<uri>     1-hop SPARQL neighborhood, capped
```

**UI (all in `ui/components/case/`):**

1. **Hypothesis tabs.** Each hypothesis = one argument subgraph, laid out bottom-up
   (change → signals → alert) on the time-ordered swimlane. The chosen hypothesis
   renders solid; alternatives render faded with their refutation badge — you see
   *in one glance* why the winner won and the others lost.
2. **Every edge is a claim.** Hover shows the selector / `wasDerivedFrom` path;
   clicking a signal node opens the SidePanel with a **[show raw lines]** button that
   calls `raw_evidence` — the audience reads the actual log lines and metric points
   behind every claim. Nothing is asserted without bytes behind it.
3. **Engine-agreement badge.** The agent's chain vs the correlation engine's chain
   (`causal_chains`) — two independent methods agreeing is the strongest single
   persuasion device the system has. Show confidence numbers from both.
4. **"Why not the others" panel.** Refuted alternatives with the *kind* of refutation
   (temporal, topology gate, explaining away, coverage) — teaching the audience the
   method, not just the answer.
5. **Time-travel replay.** The swimlane slider already exists; tie it to graph
   highlighting so the audience watches the cascade propagate up the callers as time
   advances — the "convince" moment of the demo.
6. **"This is really RDF" affordances.** Every node shows its namespace-prefixed
   type on hover; a **[Turtle]** button downloads the exact subgraph as sorted
   Turtle (same dumper as `harvest.ttl`); an **[open in SPARQL]** link loads the
   query that produced the view into the existing `/sparql` console. The graph is a
   *projection of the store*, never a parallel data structure — that is the honest
   answer to "is this just a mock?"

### B.4 Convince-the-audience checklist (what makes it "the correct RCA")

| Audience question | Where the UI answers it |
|---|---|
| "Why this change and not the one 8 minutes before?" | refutation badges + candidate_evidence facts (call-path status, minutes-before-onset) |
| "Could it be coincidence?" | time-ordered signal alignment on the swimlane; engine chain computed independently agrees |
| "Is the evidence real?" | every node → rawSelector → raw bytes; nothing in the view exists without provenance triples |
| "Does it generalize?" | Scorecard tab: eval metrics (top1, evidence_precision, trap rate, control-FP) + the ablation that shows removing the topology gate collapses accuracy (2/7) |
| "Is the graph real or a mock?" | Turtle download + preloaded SPARQL query + namespace-typed nodes |

### B.5 Sequencing

1. `hypotheses[]` in `build_case` + `case/expand` endpoint (pure backend, no UI risk).
2. Hypothesis tabs + refutation badges in CaseGraph (reuses existing layer logic).
3. raw_evidence drill-through in SidePanel, Turtle/SPARQL affordances.
4. Engine-agreement badge + time-travel highlighting.

Phase 1 (Part A) and step 1–2 (Part B) are independent and can land in parallel.

---

## Expected outcome

| | Today | Phase 1 | Phase 2 |
|---|---|---|---|
| RCA latency (same alert) | ~125–137 s | ~25–30 s | **<10 s** |
| LLM calls | 43 | ~20 | 8–9 |
| Input tokens | 410K | ~150K | ~60–80K |
| Evidence | URI text wall | — | navigable argument graph, drillable to raw bytes, engine-agreement + refutations |
