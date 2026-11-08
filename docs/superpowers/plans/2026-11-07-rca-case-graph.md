# RCA Case Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A per-incident "case view" in the UI that proves an AutoRCA result is the *right* root cause: a swimlane graph built from the real as-of RDF store, raw→processed→graph lineage on every node, suspects with computed facts and the agent's reasons, the agent's investigation trail, an honest scorecard, and an "after the agent's cut-off" ending showing the fix worked.

**Architecture:** A new backend package `e2ekg.explain` assembles JSON payloads from the as-of store (through the agent's own tools, `make_tools`), processed parquet tables and saved run/job reports; FastAPI exposes it. The Next.js UI renders it with Cytoscape.js (swimlane preset layout + fcose force layout). The agent runner persists findings and a tool-call log ("trail"). The generator gains a post-cutoff fix so degradations end and services recover. Order: case view on existing runs first (Part A), then trail (Part B), then recovery (Part C), then one regenerate + re-ingest + eval re-run (Part D).

**Tech Stack:** Python 3.12, uv, pyoxigraph, polars, FastAPI, LangChain callbacks, pytest; Next.js 16 / React 19 / TypeScript; cytoscape@3.30.x, cytoscape-fcose@2.2.x.

**Spec:** `docs/superpowers/specs/2026-11-07-rca-case-graph-design.md`. Read it first; §2 lists spike gotchas this plan relies on.

## Global Constraints

- The case view reads the **as-of** store for everything presented as "what the agent saw". The full store is read only by the after-cut-off endpoint (Task 11), and everything it returns is labelled after-cut-off.
- Nothing in `e2ekg.explain` or the API reads `raw/_answers` (the oracle). Ground truth reaches the UI only through eval results.
- Nothing hard-coded for hero: every node, edge, time and reason comes from store, processed tables or report.
- Computed facts and the agent's prose are separate fields and are rendered differently. "Vehicle, not cause" and scope arguments exist only as agent prose.
- Scorecard shows every arm, grouped `agent` (`graph`, `graph_no_evidence_tool`, `raw_only`) vs `engine` (`engine_top`, `engine_no_topology`, `engine_no_explain_away`). Never call engine ablations "naive methods the agent beats".
- Evidence precision note copy (exact): "share of cited items that are in the answer key's must-cite list".
- Slack layer note copy (exact): "claims extracted by scripted stand-in" (shown when the eval meta says chat came from the ScriptedClient).
- Inconclusive verdicts draw no root; the report's top hypothesis, if any, is a "best candidate, not accepted" node.
- Generator changes must not alter any raw record at or before `as_of_utc(scenario)`. Recovery code draws from its own RNG streams, never from existing ones.
- Commit after every task, on a feature branch (`feat/rca-case-graph`), never on `main`.

## Review Focus

1. **A report URI missing from the as-of store** (finding URIs, a misspelled or post-cutoff URI): the node is still drawn, marked `missing: true`, with no crash. Test in Task 3.
2. **Scenario with no saved run / unknown `source`**: the case endpoint returns 404 with a message naming `scripts/` or the eval command; the UI shows it. Test in Task 5.
3. **v-control (no fault, inconclusive)**: no `root` role, no onset, the best candidate shown, the swimlane still renders. Test in Task 3; checked in Task 6.
4. **A refuted string naming several changes or an unparseable string**: siblings split into separate entries; an unparseable line is kept with `uris=[]` and shown as "unattributed agent note", never dropped. Test in Task 1.
5. **Old runs without a trail** (pre-Part B runs, failed runs): the trail panel says "this run predates trail recording" instead of erroring. Test in Task 5; checked in Task 12.

---

## Part A: case view on existing runs

### Task 1: Parse the agent's refutations

**Files:**
- Create: `src/e2ekg/explain/__init__.py` (empty), `src/e2ekg/explain/refuted.py`
- Test: `tests/explain/test_refuted.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class Refuted: uris: tuple[str, ...]; reason: str; as_root: bool` and `parse_refuted(lines: list[str], known_uris: set[str] | None = None) -> list[Refuted]`.

- [ ] **Step 1: Write the failing tests** (strings copied verbatim from `data/evals/full-1run/runs/hero__graph__0.json`)

```python
def test_simple_line():
    r = parse_refuted(["https://kg.local/chg/deployment/hero/01J239A03CBAD72F638D493134 refuted: no dependency path ... landed at 22:18:00Z"])
    assert r == [Refuted(("https://kg.local/chg/deployment/hero/01J239A03CBAD72F638D493134",), "no dependency path ... landed at 22:18:00Z", False)]

def test_as_root_and_sibling_ids_are_split():
    line = ("https://kg.local/chg/scaling/hero/b9653116-19a1-ef6f-1046-404aa6e78aab (and sibling scale-downs 0edf7fa4, 04094b3a, 6a487e8e) "
            "refuted as root: the autoscaler could not take the fleet below the old floor")
    known = {"https://kg.local/chg/scaling/hero/b9653116-19a1-ef6f-1046-404aa6e78aab",
             "https://kg.local/chg/scaling/hero/0edf7fa4-aaaa", "https://kg.local/chg/scaling/hero/04094b3a-bbbb",
             "https://kg.local/chg/scaling/hero/6a487e8e-cccc"}
    [r] = parse_refuted([line], known)
    assert r.as_root and len(r.uris) == 4 and r.reason.startswith("the autoscaler")

def test_unparseable_line_is_kept():
    [r] = parse_refuted(["the gateway was never plausible"])
    assert r.uris == () and r.reason == "the gateway was never plausible"
```

- [ ] **Step 2: Run** `uv run pytest tests/explain/test_refuted.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `parse_refuted`. Leading URI, optional parenthetical, then `refuted( as root)?:`. Hex prefixes in the parenthetical resolve to the `known_uris` member whose last path segment starts with that prefix; unresolved prefixes are dropped.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat(explain): parse the agent's refuted alternatives`.

### Task 2: Raw → processed → graph lineage for one node

**Files:**
- Create: `src/e2ekg/explain/lineage.py`
- Test: `tests/explain/test_lineage.py`, `tests/explain/conftest.py` (fixture `live_tools`: `make_tools(copy_store(harvested_hero_as_of.store), preprocessed_hero.proc, "hero", "explain", llm=scripted_llm)`, same pattern as `tests/agent/conftest.py`)

**Interfaces:**
- Produces: `lineage(tools: dict, proc_root: Path, uri: str, limit: int = 3) -> dict` returning
  `{"uri", "graph": <named graph tail or None>, "records": [{"raw_id", "file", "offset", "length", "sha", "line", "ts_utc", "processed": {"table", "ts_raw", "skew_ms", "tz_hint", "app_method", "app_confidence", "template"} | None}]}`.
  `raw_id` format is `source|rel_path|offset|length|sha`. The processed row is looked up by `raw_id` in `<proc_root>/<table>.parquet`, where table is chosen by the source prefix (`logs`→`logs`, `atlas`→`metrics_atlas`, `prom`→`metrics_prom`, `traces`→`spans`, `events`→`events`, `slack`→`slack`). Columns that are absent come back as null.

- [ ] **Step 1: Write failing tests:**
  - `test_config_change_lineage`: for the root config change URI in the fixture (find it with SPARQL `?c a ops:ConfigChange ; chg:property ?p FILTER(STRENDS(?p,"minInstances"))`), `records[0]["file"] == "events/properties.log"`, `"minInstances" in records[0]["line"]`, `records[0]["processed"]["table"] == "events"`.
  - `test_log_spike_lineage_shows_timestamp_normalisation`: for any `ops:LogSpike`, every record has `processed["ts_raw"]` and `ts_utc` set.
  - `test_unknown_uri_returns_no_records`: `lineage(..., "https://kg.local/ops/finding/hero/nope")["records"] == []`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** `lineage`: `tools["raw_evidence"].invoke({"uri_or_selector": uri, "limit": limit})`, where output not starting with `[` means no records. Join with polars `scan_parquet(...).filter(pl.col("raw_id") == rid)`. Read the graph tail from the subject's quads (named graph `https://kg.local/graph/<tail>/<scenario>`).
- [ ] **Step 4: Run** them. Expected: PASS.
- [ ] **Step 5: Commit** `feat(explain): raw-to-graph lineage for a node`.

### Task 3: The case payload

**Files:**
- Create: `src/e2ekg/explain/case.py`
- Test: `tests/explain/test_case.py`

**Interfaces:**
- Consumes: `parse_refuted` (Task 1); agent tools `find_symptom`, `dependency_closure`, `changes_in_window`, `candidate_evidence`, `chat_claims` (arg names: `alert_id`; `app`, `direction`; `apps`, `start`, `end`; `alert_id`, `candidate_uris`; `channel`, `kinds`).
- Produces: `build_case(store: Store, tools: dict, scenario: str, run: dict, *, chat_note: str | None = None) -> dict`, where `run` is a saved run dict (`report`, `alert`, `arm`, `seconds`, `model_calls`, `tools`, optional `trail`). Returns:
  - `meta`: `{scenario, verdict, alert{...find_symptom.alert}, incident, as_of, onset_at (candidate_evidence symptom.onset.at, or null), summary, confidence, arm, seconds, model_calls, tool_counts, chat_note, has_trail}`
  - `nodes`: `{id, label, cls, cat, roles: list[str], app, at, hop, props, graph, missing, agent_reason, facts, discussed, confidence}`. `cat` ∈ `app|change|metric|trace|log|alert|incident|chat|finding|raw|other`. Every non-app, non-raw node that has raw evidence gets `prov` edges to `raw:<rel_path>` nodes (`cat="raw"`, `roles=["raw"]`, `label`=file name), one per distinct file from `raw_evidence(limit=3)`. `roles` ⊆ `root|chain|evidence|suspect|refuted_as_root|best_candidate|symptom|app|chat|raw`. App nodes carry `hops` (signed: upstream negative, `null` = no dependency path) and `first_degraded` (earliest `at` of metric/trace/log nodes with role chain/evidence on that app; never from changes or the incident).
  - `edges`: `{id, source, target, kind}` with `kind` ∈ `dependsOn|onApp|caller|causal|opened|mentions|cites|prov`.
  - `trail`: `run.get("trail")` passed through, or `null`.
- Rules:
  - The suspect set is `changes_in_window(apps=closure of the alert app, down, start=alert_at-6h, end=as_of)` plus every URI from `parse_refuted`.
  - `facts` come from one `candidate_evidence` call over all suspects plus the root: `{minutes_before_onset, has_path: call_path is not None, path: [caller→callee …]}`.
  - `agent_reason` is filled from `Refuted.reason`, and `discussed=False` when the agent never named the change.
  - The incident node's `app` is the alert's app, because the incident's `affects` is the humans' guess.
  - Finding URIs (`/ops/finding/`) become `cat="finding"` nodes, enriched from `run.trail.findings` when present.
  - Chat nodes are kinds hypothesis/mitigation/retraction/root_cause from `chat_claims(channel=<incident id>)`.
  - Query `dependsOn` with `use_default_graph_as_union=True`.

- [ ] **Step 1: Write failing tests.** The test report is built from the fixture's engine output so URIs exist: `chains = json.loads(tools["causal_chains"].invoke({"symptom_uri": h.symptom}))`, root = `chains[0]["links"][0][3]`, chain = the link URIs. Refuted = every other change URI from `changes_in_window`, with reason `"after onset"`.
  - `test_chain_and_root_roles`: the root node has `root` and `chain`; every chain URI is a node; `causal` edges number `len(chain)-1` plus one to the incident.
  - `test_every_window_change_is_a_suspect_with_facts`: every `changes_in_window` URI except the root has `suspect` and a `facts["has_path"]` boolean.
  - `test_undiscussed_suspect`: drop one URI from refuted → that node has `discussed is False` and `agent_reason is None`.
  - `test_roles_are_a_set`: put a chain URI in refuted with `refuted as root:` → the node has both `chain` and `refuted_as_root`.
  - `test_first_degraded_ignores_changes_and_incident`: the root app's `first_degraded` > root change `at`; the incident's `affects` app (fixture: api-gateway) has `first_degraded is None`.
  - `test_raw_file_nodes`: the root config change has a `prov` edge to `raw:events/properties.log`.
  - `test_missing_uri_is_drawn_not_fatal`: add `https://kg.local/ops/finding/hero/deadbeef` to evidence → the node exists with `missing is True` and `cat == "finding"`.
  - `test_inconclusive_has_no_root` (Review Focus 3): report `verdict="inconclusive"` with one hypothesis at confidence 0.1 → no node has `root`; that hypothesis's root has `best_candidate` and `confidence == 0.1`.
  - `test_never_reads_answers`: `monkeypatch.setattr(Path, "read_text", guard)` where `guard` raises if `"_answers"` is in the path → `build_case` succeeds.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** `build_case` (port of the spike builder, spec §2, plus the rules above).
- [ ] **Step 4: Run** them, then the full suite `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(explain): case graph payload from store, tools and report`.

### Task 4: Scorecard from eval results

**Files:**
- Create: `src/e2ekg/explain/scorecard.py`
- Test: `tests/explain/test_scorecard.py`

**Interfaces:**
- Produces: `scorecard(run_dir: Path, scenario: str) -> dict` returning
  - `{"run", "arms": [{"arm", "group": "agent"|"engine", "scenarios", "top1", "top3", "evidence_precision", "trap_rate", "control_fp_rate"}], "this_scenario": [{"arm", "group", "top1", "verdict", "root_app", "trap", "evidence_precision"}], "notes": {"evidence_precision": "share of cited items that are in the answer key's must-cite list", "chat": <meta chat note or null>}}`
  - It reads `results.parquet` and `meta.json`. Means are over runs, nulls are skipped, and `failed` rows count as misses (as in `summary.md`).

- [ ] **Step 1: Write failing tests** on a 4-row DataFrame written to `tmp_path/results.parquet`: `graph` and `engine_top` on scenarios `hero` and `v-control`.
  - `test_groups`: `graph` is in `agent` and `engine_top` is in `engine`.
  - `test_means`: compare against hand-computed values.
  - `test_this_scenario_filter`: only `hero` rows appear.
  - `test_note_copy`: exact string.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** them. Expected: PASS.
- [ ] **Step 5: Commit** `feat(explain): honest per-arm scorecard`.

### Task 5: API endpoints

**Files:**
- Modify: `src/e2ekg/api/app.py` (add endpoints after `/api/scenarios/{scenario}/alerts`; extend the module docstring's endpoint list)
- Test: `tests/api/test_case_api.py`

**Interfaces:**
- Consumes: `build_case`, `lineage`, `scorecard`.
- Produces:
  - `GET /api/scenarios/{s}/case?source=` returns the `build_case` payload. `source` is `eval:<run>` (file `data/evals/<run>/runs/<s>__graph__0.json`), `job:<id>` (a `JOBS` entry with state `done`: `{report, alert, trail?}`), or empty (the newest eval run dir by mtime that has that file). `chat_note` is the exact copy when the run's `meta.json` mentions `ScriptedClient`.
  - `GET /api/scenarios/{s}/lineage?uri=` returns the `lineage` payload.
  - `GET /api/scenarios/{s}/scorecard?run=` returns the `scorecard` payload (default: the same run as `case`).
  - Errors are `HTTPException(404, ...)`. The message for no saved run names `uv run e2ekg eval` (check the actual CLI name in `src/e2ekg/cli.py`) and the AutoRCA tab.
- Test setup: build a tmp data root by symlinking the fixtures:
  - `graph/hero/as_of` → `harvested_hero_as_of.out`, plus `harvest.json` with its `as_of`
  - `processed/hero` → `preprocessed_hero.proc`
  - `evals/r1/runs/hero__graph__0.json`, from the Task 3 engine-built report
  - `evals/r1/meta.json`
  
  Then `monkeypatch.setattr(app_module, "DATA", root)`, clear the `_store`/`_tools_cached` caches, and use `fastapi.testclient.TestClient`.

- [ ] **Step 1: Write failing tests:**
  - `test_case_default_source`: 200; `meta.scenario == "hero"`; at least one node has `root`.
  - `test_case_unknown_source_404` (Review Focus 2): `source=eval:nope` → 404.
  - `test_case_without_trail` (Review Focus 5): `meta.has_trail is False` and `trail is None`.
  - `test_lineage_endpoint`: the root URI → `records[0]["file"] == "events/properties.log"`.
  - `test_scorecard_endpoint`: 200, with `arms` present.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the endpoints, with a `_run_for(scenario, source) -> tuple[dict, Path | None]` helper.
- [ ] **Step 4: Run** `uv run pytest tests/api -v`, then the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat(api): case, lineage and scorecard endpoints`.

### Task 6: Case page with the swimlane graph

**Files:**
- Modify: `ui/package.json` (add `cytoscape@^3.30.2`, `cytoscape-fcose@^2.2.0`, `@types/cytoscape`), `ui/lib/api.ts` (types `CaseNode`, `CaseEdge`, `CaseMeta`, `CasePayload`, `Lineage`), `ui/app/scenarios/[name]/page.tsx` (link "Open case view →" in `Header`)
- Create: `ui/app/scenarios/[name]/case/page.tsx`, `ui/components/case/CaseGraph.tsx`, `ui/components/case/swimlane.ts`, `ui/components/case/SidePanel.tsx`, `ui/components/case/Toolbar.tsx`

**Interfaces:**
- Consumes: Task 5 endpoints through `useApi` / `api` in `ui/lib/api.ts`.
- Produces:
  - `swimlane(nodes: CaseNode[], opts: {t0: number; t1: number; width: number; lane: number}) -> Record<string, {x: number; y: number}>`, a pure function.
    - Lanes are apps with non-null `hops`, sorted by `hops` then id, top to bottom. Apps with null `hops` go in a bottom band.
    - x = linear time over `[onset − 20 min, as_of]`. Earlier items are compressed into the left 40 px.
    - Same-lane items within 22 px stack at ±17 px.
    - Chat nodes go in a top band and raw nodes in a bottom band, ordered by the mean x of their sources.
  - `CaseGraph` props: `{data: CasePayload; layout: "swim"|"force"; layers: {suspects: boolean; raw: boolean; chat: boolean; topology: boolean}; time: number | null; trace: number | null; onSelect(n: CaseNode): void}`.
- Behaviour:
  - Default layout is swimlane. Markers: config/root change, onset, alert, cut-off (dashed vertical lines with labels), plus 5-minute ticks.
  - Band label (exact): "services with NO dependency path to <alert app>: changes here cannot explain the symptom".
  - Replay: the slider over `[t0, as_of]` and a play button. Nodes after `time` are faded to 0.06. Apps turn red once `first_degraded <= time`.
  - Trace: steps through `hop` order every 1.1 s, highlighting each hop and showing it in the side panel.
  - Side panel:
    - Roles banner, styled per role (computed facts in a "Facts" box; agent prose in a quoted "Agent's reasoning" box; "not discussed by the agent" when `discussed === false`).
    - Properties.
    - Lineage, fetched lazily from `/lineage?uri=` and showing raw file, byte offset, sha, original line, ts_raw → ts_utc, skew, app resolution.
  - Styling follows the spike (spec §7).

- [ ] **Step 1: Install** with `cd ui && npm install cytoscape@^3.30.2 cytoscape-fcose@^2.2.0 @types/cytoscape`.
- [ ] **Step 2: Implement** `swimlane.ts`, then the components and page.
- [ ] **Step 3: Verify the build:** `cd ui && npm run typecheck && npm run build`. Expected: no errors.
- [ ] **Step 4: Verify in the real app:** start `scripts/start-backend.sh` and `scripts/start-ui.sh`, then take headless screenshots:
  `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --window-size=1600,950 --virtual-time-budget=8000 --screenshot=<scratchpad>/case-hero.png http://localhost:3000/scenarios/hero/case`, and the same for `v-control`.
  Expected:
  - hero: ★ root bottom-left before the onset line, and api-gateway in the no-path band.
  - v-control: no ★, and a "best candidate, not accepted" node (Review Focus 3).
- [ ] **Step 5: Commit** `feat(ui): case view with swimlane graph, replay, trace and lineage panel`.

## Part B: agent trail

### Task 7: Record findings and tool calls

**Files:**
- Create: `src/e2ekg/agent/trail.py`
- Modify: `src/e2ekg/agent/autorca.py` (`build_autorca` gains kw `work: Store | None = None`, used instead of `_private_copy(store)` when given; `run_rca` gains kw `trail: Trail | None = None`, appends it to callbacks, builds `work = _private_copy(...)` itself, and after `invoke`, also on failure, sets `trail.findings = findings_of(work, scenario)`)
- Test: `tests/agent/test_trail.py`

**Interfaces:**
- Produces:
  - `class Trail(BaseCallbackHandler)` with `steps: list[dict]` (`{"i", "at", "tool", "args", "output"}`, where output is truncated to 2000 chars and filled in `on_tool_end` by `run_id`) and `findings: list[dict]`.
  - `Trail.dump() -> dict` returns `{"steps": [...], "findings": [...]}`.
  - `findings_of(store: Store, scenario: str) -> list[dict]` reads every `ops:Finding` in graphs prefixed `https://kg.local/graph/findings/<scenario>/` and returns `{"uri", "specialist" (graph tail), "app", "claim", "confidence", "cites": [...]}`, sorted by uri.

- [ ] **Step 1: Write failing tests.** Reuse the `scripted_run` pattern from `tests/agent/test_autorca.py`: pass `trail=Trail()` to `run_rca` with `fake_tool_model(investigator(...))`.
  - `test_trail_has_ordered_tool_steps`: `steps[0]["tool"] == "find_symptom"`; `i` is strictly increasing; every step has `output`.
  - `test_trail_findings_match_record_finding_calls`: the number of `record_finding` steps whose output contains `"uri"` equals `len(findings)`; every finding has non-empty `cites`.
  - `test_store_on_disk_untouched`: the as-of store's quad count is unchanged after the run.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** them, then `uv run pytest tests/agent -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(agent): record the investigation trail and findings`.

### Task 8: Save the trail from evals and live jobs

**Files:**
- Modify: `src/e2ekg/evals/run.py` (`run_agent`: for arms other than `raw_only`, create `Trail()`, pass `trail=`, and add `"trail": trail.dump()` to the returned dict), `src/e2ekg/api/app.py` (`_run_job`: pass a `Trail`, set `job["trail"] = trail.dump()`; `job()` keeps returning it; `jobs()` excludes it like `report`)
- Test: `tests/evals/test_run.py` (add), `tests/api/test_case_api.py` (add)

- [ ] **Step 1: Write failing tests:**
  - `test_agent_run_saves_trail`: follow the existing scripted-model pattern in `tests/evals/test_run.py`; the run JSON has `trail.steps` and `trail.findings`.
  - `test_case_from_job_has_trail`: inject a finished job into `JOBS` with a trail → `/case?source=job:<id>` has `meta.has_trail is True` and finding nodes with `claim`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat(evals,api): persist the agent trail with each report`.

## Part C: recovery after the cut-off

### Task 9: Degradations end when the fix lands

**Files:**
- Modify: `src/e2ekg/scenario/model.py` (`Fault.fix_change_id: str | None = None`; `AnswerKey.fix_change_id: str | None = None`, `AnswerKey.recovered_utc: datetime | None = None`; the validator checks `fix_change_id` is a known change), `src/e2ekg/scenario/propagation.py` (`Degradation.end_utc: datetime | None = None`; `propagate(fault, edges, window, fixed_utc: datetime | None = None)`)
- Test: `tests/scenario/test_propagation.py` (add)

**Interfaces:**
- Produces:
  - `RECOVERY_DELAY_S: dict[FaultKind, int] = {"capacity_reduction": 600, "bad_deploy": 300, "cache_eviction_storm": 420, "db_hot_partition": 300, "cert_expiry": 120, "retry_storm": 240, "none": 0}`.
  - With `fixed_utc`, each degradation at caller depth `d` gets `end_utc = fixed_utc + RECOVERY_DELAY_S[kind] + sum(HOP_LAG_S[:d])`, and load_shed ends with its root's end + `LOAD_SHED_AFTER_CLIENTS_S`. Without it, `end_utc` is None and the output is identical to today's.

- [ ] **Step 1: Write failing tests:**
  - `test_no_fix_is_unchanged`: equals today's output for the hero.
  - `test_recovery_order_mirrors_onset`: deeper callers end later, and the target ends first.
  - `test_end_after_start`: every `end_utc` is greater than its `start_utc`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/scenario -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(scenario): degradations recover after a fix`.

### Task 10: Generator renders recovery, pre-cutoff bytes unchanged

**Files:**
- Modify: `src/e2ekg/generate/world.py` (add `active(t, d) -> np.ndarray`, which is `ramp(t, epoch(d.start_utc)) * (1 - ramp(t, epoch(d.end_utc)))` when `end_utc` is set, and use it everywhere `ramp(t, epoch(d.start_utc))` is applied to a Degradation, at lines ≈142, 171, 199, 205; `mitigation_attempts` takes `floor` and `asg` from scaling changes **before** `as_of_utc(s)` only, and drops attempts at or after the fix), `src/e2ekg/generate/render_metrics.py` (≈ lines 64, 102, 160: fault-wide ramps also end at the fix + `RECOVERY_DELAY_S`), `src/e2ekg/generate/render_traces.py` (≈ line 238, same), `src/e2ekg/generate/render_logs.py` (`_plant_degradations` plants only within `[start, end)`), `src/e2ekg/generate/run.py` (`propagate(..., fixed_utc=fix.at_utc)` when `fault.fix_change_id`; write `fix_change_id` and `recovered_utc` into `_answers/<name>.json`)
- Test: `tests/generate/test_recovery.py`

- [ ] **Step 1: Write failing tests** on a small synthetic scenario. Use `hero_scenario()` with a fix added through `model_copy`: a config change 12→48 at 17:20 PT plus scale-outs 12→24→36→48→56 at 17:21/23/25/27 PT, at `FIXTURE_SCALE`.
  - `test_capacity_recovers`: `asg.instances.inservice` for title-metadata-svc ≥ 48 by 17:35 PT.
  - `test_ttr_recovers`: the mean `client.ttr.p90` over 17:50–18:00 PT is within 10% of its mean over 13:00–14:00 PT.
  - `test_pre_cutoff_records_identical`: generate with and without the fix into two tmp dirs. For every raw file, the records whose parsed time is ≤ `as_of_utc` are byte-identical. Use the generator's own record iteration in `generate/records.py` if it exposes timestamps; otherwise compare whole files truncated at the first line after the cutoff.
  - `test_failed_bumps_still_reset_to_floor`: `mitigation_attempts` still returns 2 attempts with `floor == 12`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** Any new random draw uses `np.random.default_rng((seed, "recovery", app))`.
- [ ] **Step 4: Run** `uv run pytest tests/generate tests/scenario -q`, then the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat(generate): render recovery after the fix without touching pre-cutoff data`.

### Task 11: Scenario fixes and the after-cut-off endpoint

**Files:**
- Modify:
  - `src/e2ekg/scenario/hero.py`: fix change `chg-cfg-0002` (title-metadata-svc minInstances 12→48 at 17:20 PT), plus scale-outs `chg-asg-0007..0010` (12→24→36→48→56 at 17:21/23/25/27 PT); `Fault.fix_change_id="chg-cfg-0002"`; `AnswerKey.fix_change_id` and `recovered_utc`, the latest degradation `end_utc`.
  - `src/e2ekg/generate/hero_chat.py`: the 17:20 line becomes "owner approved, applying the revert now with the warm-up plan"; the 17:45 line becomes "status: TMS capacity was cut by a fast property, revert is out, TMS back at 56 and TTR recovering". Both are after the cut-off.
  - `src/e2ekg/scenario/variants.py`: add `_Spec.fix: tuple | None`, an `(id, kind, app, offset_min_from_fault_start=150, attrs)`:
    - v-bad-deploy: `deployment lolomo-svc {"version": "lolomo-svc-v5.88.0", "strategy": "rollback"}`
    - v-cache-storm: `config_change evcache-titles.ttl.seconds 60→86400`
    - v-hot-partition: `config_change cassandra-titles.titles.partition_key region→titleId`
    - v-cert-expiry: `config_change entitlement-svc.tls.cert.serial 0xB77E→0xC9D2 {"note": "re-issued cert"}`
    - v-retry-storm: `deployment row-ranker {"version": "row-ranker-v12.6.3", "strategy": "rollback"}`
    - v-control: `None`
- Create: `src/e2ekg/explain/after.py`; endpoint `GET /api/scenarios/{s}/after?source=` in `app.py`.
- Test: `tests/scenario/test_fixes.py`, `tests/explain/test_after.py`

**Interfaces:**
- Produces: `after_cutoff(full_store: Store, proc_root: Path, case_meta: dict, chain_uris: list[str]) -> dict` returning
  - `{"label": "after the agent's data cut-off", "as_of", "series": [{"name", "app", "points": [[iso, value], ...]}], "changes": [{"uri", "kind", "app", "at", "what"}]}`
  - `series`: the alert's metric (`case_meta.alert.metric`) and the metric of each chain `ops:Anomaly` (the key before `|` in its `rawSelector` JSON `key`) on that anomaly's app. Each is a one-minute mean from `metrics_atlas.parquet` ∪ `metrics_prom.parquet` over `[onset − 30 min, window end]`.
  - `changes`: `changes_in_window` on the full store over `(as_of, window end]` for the closure apps. This includes the failed bumps and the fix.

- [ ] **Step 1: Write failing tests:**
  - `test_every_fault_variant_has_a_fix_after_cutoff`: `as_of_utc < fix.at_utc < window.end_utc - 30 min`; v-control has no fix.
  - `test_after_cutoff_shows_fix_and_recovery` (on the fixture full store, regenerated with the fix): `changes` contains a config change with `new=48`; the alert metric's last point is within 10% of its first point.
  - `test_after_only_returns_post_cutoff_changes`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** Do not change the `ScriptedClient` chat claims for pre-cutoff messages.
- [ ] **Step 4: Run** the full suite. Expected: PASS. Fixtures regenerate with the fix.
- [ ] **Step 5: Commit** `feat: post-cutoff fixes per scenario and after-cut-off endpoint`.

### Task 12: Scorecard, trail and after-cut-off panels in the UI

**Files:**
- Create: `ui/components/case/Scorecard.tsx`, `ui/components/case/TrailPanel.tsx`, `ui/components/case/AfterPanel.tsx`
- Modify: `ui/app/scenarios/[name]/case/page.tsx`, `ui/lib/api.ts` (types `Scorecard`, `Trail`, `After`)

**Behaviour:**
- **Scorecard**: a strip with two groups, "LLM agent arms" and "Correlation engine arms (no LLM)". Each shows top-1, top-3, evidence precision (exact note copy as a tooltip/footnote), trap rate and this scenario's verdict. Headline text is computed from the data, never hard-coded: "With the graph's topology, <arms at 100% top-1> find the right root in <n>/<n>; without topology gating the engine gets <x>/<n>; the agent over raw files gets <y>/<n>."
- **Trail**:
  - Lists steps in order: tool, args (collapsed), output (collapsed), and specialist findings with claim and confidence.
  - Clicking a finding highlights its `cites` on the graph.
  - With no trail: "This run predates trail recording." (Review Focus 5)
- **After**:
  - A toggle "Show what happened after the agent's cut-off" draws a small line chart per series (inline SVG; follow the `dataviz` skill), with a shaded area after `as_of` labelled "after the agent's data cut-off" and vertical markers for each post-cutoff change.
  - Hidden when `changes` is empty (v-control).

- [ ] **Step 1: Implement** the three components and wire them into the page.
- [ ] **Step 2: Verify the build:** `cd ui && npm run typecheck && npm run build`. Expected: no errors.
- [ ] **Step 3: Commit** `feat(ui): scorecard, agent trail and after-cut-off panels`.

## Part D: regenerate, re-run, demo check

### Task 13: Regenerate, re-ingest, re-run evals, verify end to end

- [ ] **Step 1: Snapshot the old as-of graphs:** `for s in data/graph/*/; do cp "$s/as_of/harvest.ttl" "<scratchpad>/asof-before-$(basename $s).ttl"; done`.
- [ ] **Step 2: Regenerate and ingest everything:** `scripts/ingest.sh` (regenerate the raw data first if the script doesn't; check `scripts/ingest.sh` and `src/e2ekg/cli.py`). Expected: SHACL ok for all 7 scenarios.
- [ ] **Step 3: Check the as-of graphs are unchanged:** `diff` each new `data/graph/<s>/as_of/harvest.ttl` against its snapshot. Expected: no differences. If any differ, stop and fix Task 10. Do not re-run the evals on changed inputs without telling the user.
- [ ] **Step 4: Re-run the evals** with the CLI in `src/e2ekg/cli.py`: all scenarios, arms `graph`, `engine_top`, `engine_no_topology`, `engine_no_explain_away`, 1 run, into a new run dir. If any arm hits Fireworks 429, resume the same run dir. Expected: `summary.md` written, and the `graph` runs contain `trail`.
- [ ] **Step 5: Demo check.** Start the backend and UI. Take headless screenshots of `/scenarios/<s>/case` for all 7 scenarios, the hero with "after cut-off" on, and the hero with Raw lineage and Slack layers on. Read every screenshot. Expected:
  - hero: root before onset; api-gateway in the no-path band; the after panel shows the failed bumps, the fix, and TTR recovering.
  - v-control: no root.
  - Every variant: its own root and chain.
- [ ] **Step 6: Commit** the code and docs only (data is not tracked; check `.gitignore`): `chore: regenerate with recovery, re-run evals with trails`.
