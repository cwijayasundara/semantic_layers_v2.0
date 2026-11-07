# RCA Case Graph: convincing a customer it's the *right* root cause

Date: 2026-11-07. Status: draft for approval.
Builds on `docs/DESIGN.md` and `docs/superpowers/specs/2026-11-06-e2e-knowledge-graph.md`.

## 1. Intent and why

We will walk an enterprise customer (think Dell's SRE/ops lead) through our AutoRCA live in the UI.
That person has seen "AI found your root cause" demos before. Their doubt is: *how do I know this is
the right cause and not the loudest alert, the first red herring, or something the LLM made up?*

The user's bar, in their words: **evidence, traces, where the data came from, reasons, and how
these data items connect to each other to reach the RCA**, for each alert/incident.

Prior art converges on one idea: people are convinced by reasoning they can *check*, not by a
confident answer. Dynatrace replays propagation on the topology; Datadog Watchdog shows a small
causal graph with per-node evidence; Causely argues by "explains the most symptoms"; Kepner-Tregoe
uses Is / Is-Not to rule suspects out; incident investigations go facts → timeline → hypotheses
eliminated. This design puts all of that on **one interactive graph built from the real RDF store**.

The data model already supports it (see `docs/DESIGN.md` §provenance): every derived triple has
`ops:rawSelector` back to byte ranges in raw files; processed parquet rows keep `ts_raw`, `ts_utc`,
`skew_ms`, `app_method`, `app_confidence`; the agent's report has a causal chain of graph URIs and
a written reason for every refuted alternative. What is missing is a view that shows it.

## 2. What was validated by a spike

A throwaway prototype (`scratchpad/build_graph.py` + a Cytoscape.js page, not in the repo; the
builder can recreate the logic from this section) built the hero incident graph from the as-of
store and `data/evals/full-1run/runs/hero__graph__0.json`: 109 nodes, 152 edges (51 apps, 13-hop
chain, 10 suspects with reasons, 23 raw files, 8 Slack claims). Lessons:

- **A force-directed layout alone does not persuade.** It's a hairball.
- **A swimlane layout does.** x = time, y = one lane per service ordered by call depth from the
  symptom app (clients on top, deepest dependency at the bottom). The root (★ config change 21:52)
  sits bottom-left, before a vertical "onset 22:03" line; the chain climbs to the alert at 22:30.
  Suspects land visibly *after* the onset line, or in a separate bottom band labelled
  "no dependency path to the symptom app". That is the Is/Is-Not argument, drawn.
- Gotchas found: query with `use_default_graph_as_union=True` (data is in named graphs);
  `dependency_closure` up-direction hops must be negated to order lanes; the incident's
  `affects` (api-gateway, the humans' wrong first guess) must not colour that service as degraded;
  "first degraded" per service must use telemetry signals only (metric/trace/log), not changes;
  `raw_evidence` takes `uri_or_selector`; findings (`ops/finding/...`) are not in the stored graph
  (they are run-time), so render them from the report, not the store.

## 3. Goals

1. **One case view per alert/incident, for every scenario** (hero + the 6 variants including
   `v-control`, where the right answer is "no root cause found" and the view must show why each
   candidate failed).
2. **The graph is the argument.** Swimlane view by default, force-directed view as an alternative
   ("this is the knowledge graph").
3. **Every node is clickable down to raw data**, showing three-step lineage:
   raw (file, byte offset, length, hash, original line as written) →
   processed (row: `ts_raw` → `ts_utc`, `skew_ms` correction, app resolution `app_method` /
   `app_confidence`, log template) →
   graph (named graph / enricher, class, properties).
4. **Every suspect shows why it was ruled out**, in the agent's words, plus the structured tests
   it failed (landed after onset; no dependency path; scope mismatch e.g. single-platform release vs
   all-platform symptom; is a vehicle of the root, not a cause).
5. **Replay**: a time slider + play button; services turn red at their first anomaly, nodes appear
   when they happened.
6. **Trace the chain**: step through hops root → symptom; side panel shows each hop's evidence.
7. **Layers** you can toggle: suspects, raw lineage, Slack claims (including wrong human guesses
   and retractions), full topology.
8. **Scorecard strip** on the page: for this scenario, how each eval arm did (graph agent vs
   raw-only vs no-topology vs no-explain-away), plus aggregate across scenarios — "it isn't
   just *an* RCA; it's measured against an answer key and beats naive methods".
9. **Demo-safe**: works from a saved run with no LLM call; optionally from a live AutoRCA job.
10. **The agent's investigation trail**: a panel listing, in order, each tool call the agent made
    (tool, arguments, short result) and each finding it recorded (specialist, app, claim,
    confidence, cited signal URIs). Clicking a finding highlights its cited nodes on the graph.
11. **"After the agent's cut-off" ending**: a toggle that loads the full-history store for the
    period after `as_of` and shows, clearly marked as after the cut-off, that the wrong mitigation
    (api-gateway rollback) changed nothing and the right one (restore the capacity floor) brought
    the symptom metric back to baseline.

## 4. Non-goals

- Changing the agent's reasoning, prompts, or `RCAReport` schema. (Persisting what the agent
  already produces is in scope; see §6.)
- Improving evidence precision (currently 28%). Out of scope, but see §8.
- A general-purpose graph explorer / SPARQL visualiser (the SPARQL page stays as is).
- Authentication, multi-tenant, sharing.

## 5. Anti-goals (fail even if it "works")

- Anything hand-drawn or hard-coded for the hero scenario. Every node, edge, time and reason must
  come from the store, processed tables, or the saved report.
- Hiding the red herrings or the humans' wrong first guess. They are the most persuasive part.
- Showing any claim not backed by a graph node or raw record.
- Reading the answer key (`raw/_answers`) in the case view. Ground truth appears only via eval
  results, labelled as such.
- Using the full-history store for anything presented as "what the agent saw". The case view reads
  the **as-of** store; anything after the cut-off must be visually marked as "after the agent's
  data cut-off".

## 6. Decided how

- **Where**: the existing Next.js UI (`ui/`) and FastAPI backend (`src/e2ekg/api/app.py`). A new
  case view per scenario, reachable from the scenario page. (My call: route
  `/scenarios/[name]/case`, plus a link/tab from `scenarios/[name]`.)
- **Backend**: one new read endpoint that assembles the graph payload server-side (port of the
  spike's builder), going through the existing agent tools (`make_tools` via `_tools`/`_call`) so
  the view shows exactly what the agent could see. Payload: `{meta, nodes[], edges[]}` where
  nodes carry `cat` (app/change/metric/trace/log/alert/incident/chat/raw), `role`
  (root/chain/evidence/suspect/symptom/app/raw/chat), `app`, `at`, `hop`, `reason`, `props`,
  `graph`, and lineage. Raw lines are fetched lazily per node (a second endpoint) to keep the
  payload small. (My call.)
- **Report source**: `?source=eval:<run>` (default: latest eval run's
  `<scenario>__graph__0.json`) or `?source=job:<id>` for a live AutoRCA job.
- **Frontend graph library**: Cytoscape.js (+ `cytoscape-fcose` for the force view). Swimlane is a
  preset layout computed client-side. (My call, validated by the spike.)
- **Suspect set** (my call): every change in the window (`changes_in_window` over the symptom
  app's dependency closure, plus any change named in `refuted_alternatives`). One refuted string
  can name several changes ("and sibling scale-downs 0edf7fa4, …"); split those so each change is
  its own node. A suspect with no written reason from the agent still gets the computed tests and
  is labelled "not discussed by the agent".
- **Two kinds of reason, labelled differently** (my call):
  - *Computed facts* (from `candidate_evidence` and the graph): landed before/after onset (minutes),
    dependency path to the symptom app (yes/no, with the path).
  - *Agent's reasoning* (parsed from `refuted_alternatives`, `<uri> refuted[ as root]: <reason>`),
    shown as quoted prose. "Vehicle, not cause" and scope arguments (single-platform release vs
    all-platform symptom) appear **only** here, never as computed facts. Where the agent leans on
    the correlation engine's chain (`ops:chain` / `ops:link`), label it "correlation engine".
- **Roles are a set, not one value** (my call): a node can be both a chain hop and "refuted as root"
  (e.g. hero scaling 5b4aa191). The payload carries `roles: []`.
- **Inconclusive verdicts** (my call): when `verdict == "inconclusive"` (v-control), no ★ root is
  drawn. The report's weak top hypothesis (e.g. commit e8f0857 at 0.1) is drawn as "best candidate,
  not accepted", with its confidence, so the audience sees the agent declining to over-claim.
- **Slack layer label** (my call): the eval data's chat claims come from the scripted stand-in
  extractor (meta.json); the layer shows a small "claims extracted by scripted stand-in" note
  until live extraction is in place.
- **Scorecard honesty** (my call, per §5): show every arm, including `engine_top` (deterministic
  correlation engine, also 7/7) and `graph_no_evidence_tool`. Group them as "LLM agent arms" vs
  "correlation engine arms (no LLM)", and do not call engine ablations "naive methods the agent
  beats". The honest headline is: *with the knowledge graph's topology, both the agent and the
  engine find the right root 7/7; without topology gating the engine drops to 2/7; the agent over
  raw files with no graph gets 5/7.* The graph is what earns the right answer; the agent adds the
  written, checkable explanation.

- **Persist the agent trail** (user decision): the AutoRCA runner and the eval runner save,
  beside each report, (a) every `ops:Finding` the specialists recorded (URI, specialist, about app,
  claim, confidence, signal URIs), taken from the private store copy before it is discarded, and
  (b) an ordered tool-call log (tool, args, truncated output, timestamp). Live jobs keep the same
  in the job record. Then re-run the `graph` arm for all 7 scenarios to produce runs with trails.
  Expect Fireworks 429s; the runner's retry/backoff must cope.
- **Recovery in the generator** (user decision). The generator already makes the two "bump min
  to 40" attempts (16:02 and 16:31 PT) fail on purpose: the fast-property poller re-applies
  min=12 within a minute (R23). That is itself strong evidence that the root-cause property is
  still in force, so keep it. The real fix is reverting the property (12→48). Today it is only
  "applying at 17:55" PT, five minutes before the window ends, so no recovery is visible. Change:
  the revert lands at 17:20 PT, the ASG scales out, and pool exhaustion, timeouts, partial
  responses and `client.ttr.p90` return to baseline with realistic lag, consistently across
  metrics, logs and traces. Propagation is already computed from topology + fault, so recovery
  reuses it. The api-gateway rollback appears only in Slack ("rollback finished and TTR didn't
  move"); it is not added as an event, because at 22:56Z it would fall inside the agent's as-of
  data.
  Each variant scenario gets an equivalent right/wrong mitigation where its story has one;
  v-control gets none. The answer key records the mitigation times. After the change, re-ingest
  all scenarios, confirm the as-of stores (and so the agent's inputs) are unchanged up to `as_of`,
  and re-run evals.
- **Evidence precision** (user decision): shown honestly on the scorecard with a one-line note on
  what it measures ("share of cited items that are in the answer key's must-cite list"). Improving
  it is a separate project.

## 7. Left to the builder

- Visual styling, colours, node shapes (the spike used: ★ root amber, red chain, grey dashed
  diamonds for suspects, blue lanes for services, green squares for raw files, purple for Slack).
- Exact lane ordering for services not on the main path (spike: sort by signed hop count, then
  name; no-path services in a bottom band).
- Collision handling for nodes at the same time in the same lane.
- Whether the scorecard reads `results.parquet` directly or via `/api/evals/{run}`.
- "Coverage" meter (how many of the symptom-side anomalies each candidate explains), if
  `candidate_evidence` makes it cheap; otherwise skip.
- Tests: backend payload tests per scenario (every node has `at` or is an app/raw node; every
  chain URI exists in the as-of store; no node reads from the answer key; v-control yields
  `verdict: inconclusive` and no root node).

## 8. Open questions

None. The user decided recovery, the agent trail and evidence precision on 2026-11-07 (see §6).
