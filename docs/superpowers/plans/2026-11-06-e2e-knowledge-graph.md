# E2E Observability Knowledge Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a seeded generator of messy, mutually consistent MELT data with a held-out answer key. Add a lossless preprocessing pipeline, an ontology-as-code, an RDF knowledge graph with ordered enrichers and a correlation engine, and a DeepAgents AutoRCA on GLM-5.3-flash. The agent must find the true root cause, scored against the answer key.

**Architecture:** One typed `Scenario` model drives everything. Renderers turn it into raw records in memory, a messiness registry corrupts them, and a writer serializes them and records byte-exact raw ids. Preprocessing parses every byte into Parquet or quarantine and proves coverage with a ledger. Enrichers write typed triples into per-enricher named graphs in Oxigraph. Seven specialist subagents communicate only through `ops:Finding` triples.

**Tech Stack:** Python 3.12, uv, pydantic v2, numpy, polars, DuckDB, Parquet (pyarrow), drain3, pyoxigraph, rdflib, pyshacl, deepagents, langchain-fireworks, pytest.

**Spec:** `DESIGN.md`

## Global Constraints

- Python `>=3.12`, managed with `uv`. Package `e2ekg` under `src/e2ekg/`. Tests under `tests/`, mirroring `src/`.
- Everything seeded. The same `(scenario, seed)` produces byte-identical raw files. Default seed `7`.
- Raw files under `data/raw/<scenario>/` are never modified after generation.
- Raw record id format `"{source}|{rel_path}|{offset}|{length}|{sha256(payload)[:16]}"`, produced only by `e2ekg.rawid.raw_id`.
- Every byte of every raw file belongs to exactly one parsed row or one quarantine row.
- Normalized values are stored next to originals, never instead of them.
- URI scheme `https://kg.local/{ns}/{kind}/{stable_id}`. Ids come from the catalog or content hashes, never free text.
- The 12 namespaces are `ops app team infra chg exp met log trc alrt inc chat`, with prefix IRI `https://kg.local/{ns}#`.
- Every graph node has `rdf:type`. Every `ops:Application` is in the catalog. Every derived fact has `prov:wasDerivedFrom` or `ops:rawSelector`.
- Dependency edges capped at 50 per node per direction.
- Closed taxonomies. Message category in `incident question discussion decision`. Sentiment in `positive neutral negative urgent`.
- Correlation window 600 s. Chains under 0.6 confidence are discarded.
- LLM model id `fireworks:accounts/fireworks/models/glm-5p3-flash`. API key from `FIREWORKS_API_KEY`. Every LLM call goes through `e2ekg.llm.LLMClient`, cached on disk by sha256 of the request.
- No test hits the network unless marked `@pytest.mark.live`. The default `pytest` run excludes `live`.
- Hero time zone America/Los_Angeles. Hero date 2026-10-14. Window 12:00 to 18:00 PT.

## Review Focus

1. **Clock skew reorders cause and effect.** A host whose clock runs 90 s fast logs pool exhaustion before the latency it reacts to. The correlation engine must use skew-corrected time and still rank the true chain first. Test in Task 6 and Task 14.
2. **Ambiguous alias.** A surface form like `metadata` matches both `title-metadata-svc` and `artwork-metadata-svc`. It must stay an unresolved literal, never become either app. Test in Task 7.
3. **Undecodable or malformed bytes.** A latin-1 line in a UTF-8 log, or a JSON line cut mid-object. It must land in quarantine with a reason, the run must not crash, and the ledger must still balance. Test in Task 6 and Task 10.
4. **Re-running the harvest.** Running the harvest twice must give the same quad count and the same Turtle dump. Test in Task 12.
5. **LLM answers outside the contract.** The LLM returns a category not in the scheme, or invalid JSON. No triple may be emitted with that value. The message gets `chat:category chat:Unclassified` plus a quarantine reason. Test in Task 9.

---

## File Structure

```
pyproject.toml
src/e2ekg/
  rawid.py                 raw_id(), RawSpan
  timeutil.py              PT/UTC helpers shared by generator and preprocessing
  llm.py                   LLMClient protocol, FireworksClient, FakeClient, disk cache
  scenario/
    model.py               all Scenario types
    topology.py            hero catalog and dependency edges, filler apps
    propagation.py         fault -> per-app degradations
    history.py             90-day past incidents
    hero.py                hero_scenario()
    variants.py            variant_scenarios()
  generate/
    records.py             RawRecord, write_source()
    mess.py                Injector protocol, INJECTORS registry
    render_catalog.py  render_metrics.py  render_logs.py  render_traces.py
    render_events.py   render_chat.py
    run.py                 generate_scenario(), CLI entry
  preprocess/
    parsers/__init__.py    PARSERS registry keyed by source
    parsers/*.py           one module per raw format
    timefix.py             UTC normalization, skew estimation
    identity.py            Resolver
    logtemplates.py        Drain3 wrapper
    metrics_clean.py       units, resets, gaps, anomalies
    traces_repair.py       tree rebuild, edge fingerprints
    chat_extract.py        LLM claim extraction, closed outputs
    ledger.py              coverage and reachability checks
    run.py                 preprocess_scenario()
  graph/
    uris.py                uri(), NS
    store.py               Store over pyoxigraph
    enricher.py            Enricher base, EnrichmentResult, ENRICHERS order
    enrichers/*.py         ten enrichers
    harvest.py             harvest()
    shacl.py               validate()
  correlate/engine.py      correlate()
  ontology_evolve/propose.py
  agent/
    tools.py  specialists.py  autorca.py  report.py
  evals/run.py  evals/score.py
  flywheel/adapt.py
  cli.py                   `e2ekg` command
ontology/
  core.ttl  schemes/*.ttl  shapes.ttl
  cq/NN-name.rq  cq/NN-name.expected.json
  proposals/  extensions/
tests/ (mirrors src/)
```

---

### Task 1: Project scaffold and scenario model

**Files:**
- Create: `pyproject.toml`, `.gitignore` (`data/`, `.cache/`), `src/e2ekg/__init__.py`, `src/e2ekg/scenario/model.py`, `src/e2ekg/timeutil.py`
- Test: `tests/scenario/test_model.py`

**Interfaces:**
- Produces, in `e2ekg.scenario.model`, frozen pydantic v2 models:
  - `AppId = NewType("AppId", str)`
  - `AppKind = Literal["client", "edge", "service", "datastore", "cache"]`
  - `Application(id: AppId, aliases: tuple[str, ...], owner_team: str, tier: int, regions: tuple[str, ...], kind: AppKind)`
  - `DependencyEdge(caller: AppId, callee: AppId, protocol: Literal["grpc","http","graphql","cql","memcached"], pool_size: int | None)`
  - `FaultKind = Literal["capacity_reduction","bad_deploy","cache_eviction_storm","db_hot_partition","cert_expiry","retry_storm","none"]`
  - `Fault(kind: FaultKind, target: AppId | None, start_utc: datetime, trigger_change_id: str | None)`
  - `ChangeKind = Literal["deployment","config_change","scaling","experiment_allocation","commit","client_release"]`
  - `Change(id: str, kind: ChangeKind, app: AppId, at_utc: datetime, attrs: dict[str, str], is_root_cause: bool)`
  - `Window(start_utc: datetime, end_utc: datetime, metric_step_s: int)`
  - `ChainLink(signal: str, app: AppId, at_utc: datetime)` with `signal` one of the 13 signal types in the spec
  - `AnswerKey(root_cause_change_id: str | None, causal_chain: tuple[ChainLink, ...], red_herring_change_ids: tuple[str, ...], evidence_tags: tuple[str, ...])`
  - `Scenario(name: str, seed: int, catalog: tuple[Application, ...], edges: tuple[DependencyEdge, ...], window: Window, fault: Fault, changes: tuple[Change, ...], history: tuple["PastIncident", ...], answer_key: AnswerKey)`
  - `PastIncident(id: str, opened_utc: datetime, severity: Literal["SEV1","SEV2","SEV3"], affected: AppId, cause_change_kind: ChangeKind | None, cause_app: AppId | None, resolver_handle: str, summary: str)`
- Produces `e2ekg.timeutil.PT = ZoneInfo("America/Los_Angeles")`, `pt(y,m,d,h,mi,s=0) -> datetime` (aware UTC).

- [ ] **Step 1: Scaffold.** `git init`, `uv init --package --name e2ekg --python 3.12`, `uv add pydantic numpy polars duckdb pyarrow drain3 pyoxigraph rdflib pyshacl deepagents langchain-fireworks`, `uv add --dev pytest`. In `pyproject.toml` set `[tool.pytest.ini_options] addopts = "-m 'not live'"` and `markers = ["live: hits Fireworks"]`, and `[project.scripts] e2ekg = "e2ekg.cli:main"`.

- [ ] **Step 2: Write the failing tests**

```python
def test_scenario_round_trips_json():
    s = minimal_scenario()            # helper in tests/conftest.py, 3 apps, 2 edges
    assert Scenario.model_validate_json(s.model_dump_json()) == s

def test_scenario_is_frozen():
    with pytest.raises(ValidationError):
        minimal_scenario().name = "x"

def test_edge_to_unknown_app_rejected():
    with pytest.raises(ValidationError, match="unknown app"):
        minimal_scenario(extra_edge=("a", "ghost"))

def test_pt_helper_is_utc():
    assert pt(2026, 10, 14, 15, 30).isoformat() == "2026-10-14T22:30:00+00:00"
```

- [ ] **Step 3: Run them.** `uv run pytest tests/scenario/test_model.py -v`. Expected FAIL on import.
- [ ] **Step 4: Implement the models.** Add a `Scenario` `model_validator` that rejects edges and changes naming apps not in `catalog`.
- [ ] **Step 5: Run them.** Expected PASS.
- [ ] **Step 6: Commit.** `git add -A && git commit -m "feat: scaffold and scenario model"`

### Task 2: Hero topology and fault propagation

**Files:**
- Create: `src/e2ekg/scenario/topology.py`, `src/e2ekg/scenario/propagation.py`
- Test: `tests/scenario/test_topology.py`, `tests/scenario/test_propagation.py`

**Interfaces:**
- Consumes: Task 1 models.
- Produces:
  - `hero_catalog(rng: np.random.Generator) -> tuple[Application, ...]`. Exactly 150 apps. Named apps: `client-tv client-android client-ios client-web edge-gateway homepage-graphql lolomo-svc row-ranker title-batch-loader title-metadata-svc artwork-svc artwork-metadata-svc entitlement-svc api-gateway playback-api ab-allocator evcache-titles cassandra-titles ratings-svc profile-svc search-svc`. `api-gateway` is owned by `Team Bedrock` and runs in `us-east-1`. The rest are filler apps with generated names.
  - `hero_edges(catalog) -> tuple[DependencyEdge, ...]`. It must contain the path `client-* -> edge-gateway -> homepage-graphql -> lolomo-svc -> row-ranker -> title-batch-loader -> title-metadata-svc`. `title-batch-loader -> title-metadata-svc` has `pool_size=64`. `api-gateway` is reachable from clients through `playback-api` only, never downstream of `homepage-graphql`. `row-ranker -> ab-allocator`. `lolomo-svc -> artwork-svc`.
  - `downstream_closure(edges, app: AppId) -> frozenset[AppId]`
  - `Degradation(app: AppId, start_utc: datetime, kind: Literal["latency","errors","pool_exhaustion","timeouts","partial_response","load_shed"], magnitude: float)`
  - `propagate(fault: Fault, edges, window) -> tuple[Degradation, ...]`. It walks callers from `fault.target` upward. Per hop it adds a lag of `rng`-free fixed 120 to 240 s, given by `HOP_LAG_S = (180, 150, 180, 120, 150, 120)`. It emits `pool_exhaustion` at the caller whose edge has a `pool_size`, `timeouts` above it, `partial_response` at `homepage-graphql`, and `latency` at every client (TTR p90 +1.5 s). It adds `load_shed` at `edge-gateway` after the client latency starts.

- [ ] **Step 1: Write the failing tests**

```python
def test_hero_path_is_six_hops():
    e = hero_edges(hero_catalog(rng(7)))
    assert shortest_hops(e, "client-tv", "title-metadata-svc") == 6

def test_api_gateway_not_downstream_of_homepage():
    e = hero_edges(hero_catalog(rng(7)))
    assert "api-gateway" not in downstream_closure(e, "homepage-graphql")

def test_propagation_reaches_all_clients_after_pool_exhaustion():
    d = propagate(hero_fault(), hero_edges(hero_catalog(rng(7))), hero_window())
    pool = next(x for x in d if x.kind == "pool_exhaustion")
    assert pool.app == "title-batch-loader"
    clients = [x for x in d if x.app.startswith("client-") and x.kind == "latency"]
    assert {c.app for c in clients} == {"client-tv","client-android","client-ios","client-web"}
    assert all(c.start_utc > pool.start_utc for c in clients)
    assert all(abs(c.magnitude - 1.5) < 1e-9 for c in clients)

def test_every_degradation_traces_to_fault_through_edges():
    e = hero_edges(hero_catalog(rng(7)))
    for x in propagate(hero_fault(), e, hero_window()):
        assert "title-metadata-svc" in downstream_closure(e, x.app) | {x.app}
```

- [ ] **Step 2: Run them.** Expected FAIL.
- [ ] **Step 3: Implement** `topology.py` and `propagation.py`. `hero_fault()` and `hero_window()` are test helpers in `tests/conftest.py` that mirror Task 3's values.
- [ ] **Step 4: Run them.** Expected PASS.
- [ ] **Step 5: Commit.** `feat: hero topology and fault propagation`

### Task 3: Hero scenario, history and variants

**Files:**
- Create: `src/e2ekg/scenario/history.py`, `src/e2ekg/scenario/hero.py`, `src/e2ekg/scenario/variants.py`
- Test: `tests/scenario/test_hero.py`, `tests/scenario/test_history.py`, `tests/scenario/test_variants.py`

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces:
  - `hero_scenario(seed: int = 7) -> Scenario`, with these changes (PT times on 2026-10-14):
    - `chg-cfg-0001` config_change on `title-metadata-svc` at 14:52, `attrs={"property":"title-metadata-svc.asg.minInstances","old":"48","new":"12"}`, `is_root_cause=True`.
    - `chg-asg-00xx` scaling events on `title-metadata-svc` every 2 min from 15:00 to 15:10, stepping 48 to 12.
    - `chg-exp-0001` experiment_allocation on `row-ranker` at 15:05, cell B goes from 0% to 5% of members.
    - `chg-rel-0001` client_release on `client-android` at 15:12.
    - `chg-dep-0001` deployment on `api-gateway` at 15:18.
    - `fault.start_utc` = 14:52 PT. Client latency starts at 15:20. The alert fires at 15:30.
  - `answer_key.root_cause_change_id == "chg-cfg-0001"`. `red_herring_change_ids == ("chg-exp-0001","chg-rel-0001","chg-dep-0001")`. `causal_chain` follows config_change, scaling, metric_anomaly (title-metadata-svc latency), log_error (pool exhaustion in title-batch-loader), slow_trace, metric_anomaly (client TTR), alert, incident.
  - `build_history(catalog, edges, rng, days=90, end_utc) -> tuple[PastIncident, ...]`. 30 incidents. 10 of the 25 SEV2s are caused by `api-gateway` deployments (40%). 3 of those 10 have a summary containing `Connection Timeout` (30%). One SEV2 exactly 75 to 105 days before has `affected="lolomo-svc"`, `cause_change_kind="config_change"`, `cause_app="title-metadata-svc"`, and a `resolver_handle` that recurs in the hero Slack channel.
  - `variant_scenarios(seed: int = 7) -> tuple[Scenario, ...]`. Six scenarios named `v-bad-deploy` (lolomo-svc deploy), `v-cache-storm` (evcache-titles), `v-hot-partition` (cassandra-titles), `v-cert-expiry` (entitlement-svc), `v-retry-storm` (commit adding retries in row-ranker, then deploy), `v-control` (fault kind `none`, `root_cause_change_id=None`). Each keeps at least two red herrings.

- [ ] **Step 1: Write the failing tests**

```python
def test_hero_answer_key():
    s = hero_scenario()
    assert s.answer_key.root_cause_change_id == "chg-cfg-0001"
    assert set(s.answer_key.red_herring_change_ids) == {"chg-exp-0001","chg-rel-0001","chg-dep-0001"}

def test_root_cause_outside_ten_minute_window_of_symptom():
    s = hero_scenario()
    cfg = change(s, "chg-cfg-0001")
    assert (pt(2026,10,14,15,20) - cfg.at_utc).total_seconds() > 600

def test_red_herrings_inside_ten_minute_window():
    s = hero_scenario()
    for cid in ("chg-rel-0001","chg-dep-0001"):
        assert (pt(2026,10,14,15,20) - change(s, cid).at_utc).total_seconds() <= 600

def test_history_priors():
    h = hero_scenario().history
    sev2 = [i for i in h if i.severity == "SEV2"]
    gw = [i for i in sev2 if i.cause_app == "api-gateway" and i.cause_change_kind == "deployment"]
    assert len(h) == 30 and len(sev2) == 25 and len(gw) == 10
    assert sum("Connection Timeout" in i.summary for i in gw) == 3

def test_history_has_last_quarter_twin():
    h = hero_scenario().history
    twins = [i for i in h if i.cause_app == "title-metadata-svc" and i.cause_change_kind == "config_change"]
    assert len(twins) == 1

def test_control_variant_has_no_root_cause():
    v = {s.name: s for s in variant_scenarios()}
    assert v["v-control"].answer_key.root_cause_change_id is None
    assert len(v) == 6

def test_deterministic():
    assert hero_scenario(7).model_dump_json() == hero_scenario(7).model_dump_json()
```

- [ ] **Step 2: Run them.** Expected FAIL.
- [ ] **Step 3: Implement** `history.py`, `hero.py`, `variants.py`.
- [ ] **Step 4: Run them.** Expected PASS.
- [ ] **Step 5: Commit.** `feat: hero scenario, incident history, variants`

### Task 4: Raw records, writer and messiness registry

**Files:**
- Create: `src/e2ekg/rawid.py`, `src/e2ekg/generate/records.py`, `src/e2ekg/generate/mess.py`
- Test: `tests/generate/test_records.py`, `tests/generate/test_mess.py`

**Interfaces:**
- Produces:
  - `raw_id(source: str, rel_path: str, offset: int, payload: bytes) -> str` in the Global Constraints format.
  - `RawSpan(raw_id: str, source: str, rel_path: str, offset: int, length: int)`, with `RawSpan.parse(raw_id) -> RawSpan`.
  - `RawRecord(source: str, rel_path: str, payload: bytes, at_utc: datetime | None, tags: frozenset[str])`. `tags` live in memory only and are never serialized. `at_utc` is ground truth for sorting and is never written.
  - `write_source(records: Sequence[RawRecord], root: Path) -> list[tuple[str, frozenset[str]]]`. It appends records in order per `rel_path`, each payload followed by `b"\n"`, and returns `(raw_id, tags)` per record.
  - `MessInjection(injector_id: str, rel_path: str, detail: str)`
  - `class Injector(Protocol): id: str; sources: frozenset[str]; def apply(self, recs: list[RawRecord], rng, scenario) -> tuple[list[RawRecord], list[MessInjection]]`
  - `INJECTORS: tuple[Injector, ...]`, in application order, with ids `id.alias_surface id.casing id.suffix id.kind_confusion id.stale_catalog_alias id.slack_only_rename met.tag_key_drift met.unit_seconds met.counter_reset met.gap met.late met.duplicate_series log.tz_mix log.host_skew log.truncate log.duplicate log.interleave log.pii log.encoding trc.orphan trc.skew trc.zipkin_legacy trc.sampling evt.missing_field evt.duplicate_webhook chat.typo chat.nickname chat.edit chat.retraction`.
  - `apply_mess(recs, rng, scenario) -> tuple[list[RawRecord], list[MessInjection]]`. An injector that rewrites a record keeps its `tags`.
  - `log.host_skew` picks one `title-batch-loader` host and shifts its log timestamps +90 s. `log.encoding` writes one latin-1 line per log file that has non-ASCII text. `log.truncate` cuts 0.1% of JSON log lines mid-object.

- [ ] **Step 1: Write the failing tests**

```python
def test_raw_id_matches_bytes(tmp_path):
    out = write_source([rec("logs", "a.log", b"hello")], tmp_path)
    span = RawSpan.parse(out[0][0])
    assert (tmp_path / "a.log").read_bytes()[span.offset:span.offset+span.length] == b"hello"

def test_registry_ids_unique_and_complete():
    ids = [i.id for i in INJECTORS]
    assert len(ids) == len(set(ids)) == 29

def test_every_injector_fires_on_hero_fixture():
    recs, inj = apply_mess(hero_fixture_records(), rng(7), hero_scenario())
    assert {i.injector_id for i in inj} == {i.id for i in INJECTORS}

def test_tags_survive_mess():
    recs, _ = apply_mess(hero_fixture_records(), rng(7), hero_scenario())
    assert any("evidence:pool_exhaustion" in r.tags for r in recs)
```

- [ ] **Step 2: Run them.** Expected FAIL.
- [ ] **Step 3: Implement.** `hero_fixture_records()` is a small fixture in `tests/generate/conftest.py` with at least one record per source.
- [ ] **Step 4: Run them.** Expected PASS.
- [ ] **Step 5: Commit.** `feat: raw records, writer, messiness registry`

### Task 5: Signal renderers and the generate command

**Files:**
- Create: `src/e2ekg/generate/render_catalog.py`, `render_metrics.py`, `render_logs.py`, `render_traces.py`, `render_events.py`, `render_chat.py`, `run.py`, `src/e2ekg/cli.py`
- Test: `tests/generate/test_renderers.py`, `tests/generate/test_generate_run.py`

**Interfaces:**
- Consumes: Tasks 1 to 4. `propagate()` drives every renderer, so the signals agree.
- Produces: `render_<x>(scenario: Scenario, degradations: tuple[Degradation, ...], rng) -> list[RawRecord]` per module. Sources and paths:
  - `catalog` to `catalog/apps.yaml`. Aliases are deliberately incomplete. `TMS` is missing and appears only in Slack.
  - `atlas` to `metrics/atlas.jsonl`, one point per line with `{"name","tags","t","v"}`. 60 s step. TTR series `client.ttr.p90` per client platform and per `ab.cell`. A diurnal base comes from `base*(1+0.35*sin(...))`, plus Gaussian noise.
  - `prom` to `metrics/prom/<app>-<HHMM>.prom` in exposition text, every 5 min, for 20 apps.
  - `logs` to `logs/<app>/<host>.log`. Per-app format from `LOG_FORMAT = {"title-batch-loader":"log4j","row-ranker":"logfmt","edge-gateway":"nginx","homepage-graphql":"json",...}` with default `json`. Template frequencies are Zipf-distributed (s=1.3) over 20 to 40 templates per app. `artwork-svc` emits a chronic `NullPointerException` at a constant rate across the whole window.
  - `traces` to `traces/otlp/<HHMM>.json` (OTLP JSON), plus `traces/zipkin/<HHMM>.json` for two legacy apps. Head sampling 1%, plus every error trace.
  - `events` to `events/spinnaker.jsonl`, `events/properties.log`, `events/asg.jsonl`, `events/ab_allocations.csv`, `events/commits.log` (git log format), `alerts/radar.jsonl`, `pagerduty/pages.json`, `incidents/incident_io.json`. The live incident's first `affected_service` is `api-gateway`, a wrong early guess, and is corrected to `homepage-graphql` 40 min later.
  - `chat` to `slack/<channel>/messages.json` in Slack export format. Hero channel `inc-5377-homepage-ttr` has 200 messages, 50 users and 15 distinct services mentioned. Past incidents get 10 to 40 messages each.
  - Records carrying answer-key evidence get tags `evidence:<signal>`.
  - Realism constants live at the top of each renderer: `ZIPF_S = 1.3`, `DIURNAL_AMP = 0.35`, `TEMPLATES_PER_APP = (20, 40)`. They are first guesses. Calibrating them against LogHub and RCAEval RE2 is a follow-up, not part of this plan.
- Produces `generate_scenario(scenario: Scenario, out: Path) -> GenerationManifest` and writes `out/_manifest.json` containing the injections and file list. It writes `out/../_answers/<name>.json` with the answer key plus `evidence_raw_ids`. This answer file sits outside the raw tree, and no pipeline code may read it.
- CLI: `e2ekg generate --scenario {hero,v-...,all} --seed 7 --out data/raw`.

- [ ] **Step 1: Write the failing tests**

```python
def test_generate_is_byte_deterministic(tmp_path):
    generate_scenario(hero_scenario(), tmp_path/"a"); generate_scenario(hero_scenario(), tmp_path/"b")
    assert tree_hash(tmp_path/"a") == tree_hash(tmp_path/"b")

def test_hero_sizes(generated_hero):
    m = manifest(generated_hero)
    assert 2_000_000 <= m.counts["logs"] <= 5_000_000
    assert m.counts["spans"] >= 150_000
    assert slack_stats(generated_hero, "inc-5377-homepage-ttr") == (200, 50, 15)

def test_ttr_regression_cross_platform_and_cell_independent(generated_hero):
    for p in ("tv","android","ios","web"):
        assert ttr_delta(generated_hero, p) == pytest.approx(1.5, abs=0.2)
    assert abs(ttr_delta(generated_hero, "android", cell="B") - ttr_delta(generated_hero, "android", cell="A")) < 0.1

def test_answer_key_written_outside_raw_tree(generated_hero):
    assert not any("_answers" in str(p) for p in generated_hero.rglob("*"))
    assert len(load_answers("hero")["evidence_raw_ids"]) >= 8

def test_chronic_npe_flat(generated_hero):
    assert npe_rate(generated_hero, "12:00","14:00") == pytest.approx(npe_rate(generated_hero, "15:00","17:00"), rel=0.15)
```

- [ ] **Step 2: Run them.** Expected FAIL. `generated_hero` is a session-scoped fixture that generates once into `tmp_path_factory`.
- [ ] **Step 3: Implement** the renderers, `run.py` and the CLI.
- [ ] **Step 4: Run them.** Expected PASS. Then run `uv run e2ekg generate --scenario all --out data/raw` and record the wall time in the commit message.
- [ ] **Step 5: Commit.** `feat: signal renderers and generate command`

### Task 6: Lossless parsing, time normalization and the ledger

**Files:**
- Create: `src/e2ekg/preprocess/parsers/__init__.py`, `parsers/json_lines.py`, `parsers/logfmt.py`, `parsers/log4j.py`, `parsers/nginx.py`, `parsers/prom.py`, `parsers/otlp.py`, `parsers/zipkin.py`, `parsers/csv_events.py`, `parsers/gitlog.py`, `parsers/slack.py`, `parsers/yaml_catalog.py`, `src/e2ekg/preprocess/timefix.py`, `src/e2ekg/preprocess/ledger.py`, `src/e2ekg/preprocess/run.py`
- Test: `tests/preprocess/test_parsers.py`, `tests/preprocess/test_timefix.py`, `tests/preprocess/test_ledger.py`

**Interfaces:**
- Consumes: `RawSpan`, `raw_id`.
- Produces:
  - `ParsedRow` columns (all Parquet tables share them): `raw_id: str`, `source: str`, `rel_path: str`, `ts_raw: str | None`, `ts_utc: datetime | None`, `skew_ms: int`, `surface_app: str | None`, `fields: struct/json`, `_residual: json`. Source-specific columns are added per table.
  - `QuarantineRow(raw_id, source, rel_path, reason: Literal["decode_error","truncated","unparseable","unknown_format"], excerpt: str)`
  - `class Parser(Protocol): source: str; def split(self, data: bytes) -> Iterator[tuple[int,int]]; def parse(self, payload: bytes) -> dict`. `split` yields `(offset, length)` spans. `log4j.split` groups continuation lines (`^\s+at |^Caused by:`) into one span.
  - `PARSERS: dict[str, Parser]`, keyed by a file glob relative to the scenario root.
  - `estimate_skew(spans_df, logs_df) -> dict[str, int]` (host to ms). It uses trace parent/child ordering and request-id pairs between caller and callee logs, taking the median offset.
  - `normalize_ts(ts_raw: str, host_tz_hint: str | None) -> datetime` handles ISO with offset, naive PT, epoch ms, nginx `%d/%b/%Y:%H:%M:%S %z`, and log4j `yyyy-MM-dd HH:mm:ss,SSS`.
  - `check_coverage(raw_root: Path, processed_root: Path) -> CoverageReport(files: int, bytes_total: int, bytes_covered: int, gaps: list[RawSpan], overlaps: list[RawSpan])`. Newline separators count as covered by the preceding span.
  - `preprocess_scenario(raw_root: Path, processed_root: Path, llm: LLMClient) -> PreprocessReport`, writing `processed_root/<source>.parquet` and `quarantine.parquet`. Tasks 7 to 9 extend it.
- CLI: `e2ekg preprocess --scenario hero`.

- [ ] **Step 1: Write the failing tests**

```python
def test_coverage_is_total(preprocessed_hero):
    r = check_coverage(RAW/"hero", PROC/"hero")
    assert r.gaps == [] and r.overlaps == [] and r.bytes_covered == r.bytes_total

def test_latin1_line_quarantined_not_dropped(preprocessed_hero):
    q = quarantine(preprocessed_hero)
    assert (q["reason"] == "decode_error").sum() >= 1

def test_truncated_json_quarantined(preprocessed_hero):
    assert (quarantine(preprocessed_hero)["reason"] == "truncated").sum() >= 1

def test_log4j_stacktrace_is_one_row():
    spans = list(PARSERS["logs/*/*.log#log4j"].split(STACK_FIXTURE))
    assert len(spans) == 2

def test_original_timestamp_kept(preprocessed_hero):
    df = table(preprocessed_hero, "logs")
    assert df["ts_raw"].null_count() == 0 and df["ts_utc"].null_count() == 0

def test_skew_recovered(preprocessed_hero):
    skew = skew_table(preprocessed_hero)
    skewed = injected_skew_host("hero")            # read from _manifest.json
    assert skew[skewed] == pytest.approx(90_000, abs=5_000)

def test_unknown_fields_go_to_residual():
    row = PARSERS["metrics/atlas.jsonl"].parse(b'{"name":"x","tags":{},"t":1,"v":2,"zz":9}')
    assert row["_residual"] == {"zz": 9}
```

- [ ] **Step 2: Run them.** Expected FAIL.
- [ ] **Step 3: Implement** the parsers, `timefix.py`, `ledger.py` and `run.py`. Decode with `errors="strict"`, and send the span to quarantine on `UnicodeDecodeError`.
- [ ] **Step 4: Run them.** Expected PASS.
- [ ] **Step 5: Commit.** `feat: lossless parsing, time normalization, coverage ledger`

### Task 7: Identity resolution

**Files:**
- Create: `src/e2ekg/llm.py`, `src/e2ekg/preprocess/identity.py`
- Modify: `src/e2ekg/preprocess/run.py`, adding a resolve pass over every table's `surface_app`
- Test: `tests/test_llm.py`, `tests/preprocess/test_identity.py`

**Interfaces:**
- Produces in `e2ekg.llm`:
  - `class LLMClient(Protocol): def structured(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel`
  - `FireworksClient(model: str = MODEL_ID, cache_dir: Path = Path(".cache/llm"))`. It uses `ChatFireworks(...).with_structured_output(schema)`, caches by sha256 of `(model, system, user, schema.model_json_schema())`, and raises `LLMContractError` on a validation failure after 2 retries.
  - `FakeClient(responses: dict[str, BaseModel | Exception])`, keyed by sha256 like the cache.
  - `MODEL_ID = "accounts/fireworks/models/glm-5p3-flash"`
- Produces in `identity.py`:
  - `Resolution(app: AppId | None, surface: str, method: Literal["exact","alias","pattern","fuzzy","llm","unresolved"], confidence: float)`
  - `Resolver(catalog: tuple[Application, ...], llm: LLMClient | None)` with `.resolve(surface: str, context: str = "") -> Resolution`.
  - Order of attempts:
    1. Exact id or catalog alias, case-folded.
    2. Pattern strip of `-prod`, `-v\d+`, `-[a-f0-9]{5,10}$` pod hashes, ASG `-asg-\d+`, CamelCase to kebab.
    3. rapidfuzz `token_set_ratio >= 92` with a single best match and a runner-up at least 5 points lower.
    4. The LLM, choosing from a closed list of the top 5 fuzzy candidates plus `none`.
    5. Otherwise `unresolved`.
  - Added columns per table: `app_uri`, `app_method`, `app_confidence`. `surface_app` is kept.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("surface", ["title-metadata-svc","TitleMetadataSvc","titlemetadata","tms-prod-v042","title-metadata-svc-7f9c2d4e1a"])
def test_surface_forms_resolve(resolver, surface):
    assert resolver.resolve(surface).app == "title-metadata-svc"

def test_ambiguous_alias_stays_unresolved(resolver_no_llm):
    r = resolver_no_llm.resolve("metadata")
    assert r.app is None and r.method == "unresolved"

def test_slack_only_nickname_needs_llm(catalog):
    fake = FakeClient({key_for("TMS"): Pick(choice="title-metadata-svc")})
    assert Resolver(catalog, fake).resolve("TMS", context="TMS pods scaled in").method == "llm"

def test_llm_cannot_invent_app(catalog):
    fake = FakeClient({key_for("TMS"): Pick(choice="made-up-svc")})
    assert Resolver(catalog, fake).resolve("TMS").app is None

def test_team_name_never_becomes_app(resolver):
    assert resolver.resolve("Team Bedrock").app is None

def test_llm_cache_hits(tmp_path, monkeypatch): ...   # second call does not invoke ChatFireworks
```

- [ ] **Step 2: Run them.** Expected FAIL.
- [ ] **Step 3: Implement.** Add `rapidfuzz` with `uv add rapidfuzz`.
- [ ] **Step 4: Run them.** Expected PASS.
- [ ] **Step 5: Commit.** `feat: catalog-gated identity resolution and LLM client`

### Task 8: Fingerprints for logs, metrics and traces

**Files:**
- Create: `src/e2ekg/preprocess/logtemplates.py`, `src/e2ekg/preprocess/metrics_clean.py`, `src/e2ekg/preprocess/traces_repair.py`
- Modify: `src/e2ekg/preprocess/run.py`
- Test: `tests/preprocess/test_logtemplates.py`, `test_metrics_clean.py`, `test_traces_repair.py`

**Interfaces:**
- Produces:
  - `Selector(source: str, app_uri: str | None, key: str | None, t_start: datetime, t_end: datetime)`, serialized as a compact JSON string. `select(proc_root, sel) -> polars.DataFrame` runs in DuckDB.
  - `mine_templates(logs_df) -> logs_df` adding `template_id: str` (sha1 of template text, 12 hex chars), `template: str` and `params: list[str]`. It uses Drain3 with `sim_th=0.4` and `depth=4`, seeded by sorted input order.
  - `log_buckets(logs_df) -> DataFrame[app_uri, template_id, hour_start, count, error_level, selector]` covering every row.
  - `log_spikes(logs_df) -> DataFrame[app_uri, template_id, t_start, t_end, count, baseline_rate, z, selector]`. 1-min buckets, robust z (median and MAD over the trailing 60 min), `z >= 6` and `count >= 20`.
  - `clean_metrics(atlas_df, prom_df) -> DataFrame[series_key, app_uri, name, tags_norm, t, v, v_raw, unit_raw, gap: bool]`. Tag keys `nf.app|app|service` map to `app`. Seconds become ms when the name ends `_seconds` or the tag says `unit=s`. Counter resets give a monotone delta. Duplicate series are deduplicated by `(series_key, t)`, keeping the first `raw_id` and listing the others in `dup_raw_ids`.
  - `metric_anomalies(clean_df) -> DataFrame[series_key, app_uri, t_start, t_end, direction, magnitude, selector]`. Change point by a two-window median shift (30 min against 10 min) at 5 MAD or more.
  - `repair_traces(spans_df, skew) -> spans_df` adding `orphan: bool` and `ts_utc_corrected`. It maps Zipkin to the OTLP field names.
  - `edge_fingerprints(spans_df) -> DataFrame[caller_uri, callee_uri, minute, p50_ms, p99_ms, error_rate, n, selector]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_buckets_cover_every_log_row(preprocessed_hero):
    assert bucket_row_total(preprocessed_hero) == table(preprocessed_hero, "logs").height

def test_pool_exhaustion_spike_found(preprocessed_hero):
    s = spikes(preprocessed_hero)
    hit = s.filter((pl.col("app_uri") == app_uri("title-batch-loader")) & pl.col("template").str.contains("pool"))
    assert hit.height >= 1 and hit["t_start"].min() < utc("15:20")

def test_chronic_npe_not_a_spike(preprocessed_hero):
    assert spikes(preprocessed_hero).filter(pl.col("app_uri") == app_uri("artwork-svc")).height == 0

def test_seconds_normalized_original_kept(preprocessed_hero):
    m = clean(preprocessed_hero).filter(pl.col("unit_raw") == "s")
    assert (m["v"] == m["v_raw"] * 1000).all()

def test_latency_anomaly_on_title_metadata_before_clients(preprocessed_hero):
    a = anomalies(preprocessed_hero)
    tms = first_start(a, "title-metadata-svc"); tv = first_start(a, "client-tv", name="client.ttr.p90")
    assert tms < tv

def test_orphans_marked_not_dropped(preprocessed_hero):
    t = table(preprocessed_hero, "spans")
    assert t["orphan"].sum() > 0 and t.height == raw_span_count("hero")

def test_selector_round_trip(preprocessed_hero):
    row = spikes(preprocessed_hero).row(0, named=True)
    assert select(PROC/"hero", Selector.from_json(row["selector"])).height == row["count"]
```

- [ ] **Step 2: Run them.** Expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected PASS.
- [ ] **Step 5: Commit.** `feat: log, metric and trace fingerprints with raw selectors`

### Task 9: Chat extraction with closed outputs

**Files:**
- Create: `src/e2ekg/preprocess/chat_extract.py`
- Modify: `src/e2ekg/preprocess/run.py`
- Test: `tests/preprocess/test_chat_extract.py`

**Interfaces:**
- Consumes: `LLMClient`, `Resolver`, the Slack parsed table.
- Produces:
  - `ThreadLabel(category: Literal["incident","question","discussion","decision"], sentiment: Literal["positive","neutral","negative","urgent"], is_incident_related: bool)`
  - `ClaimKind = Literal["hypothesis","evidence","mitigation","retraction","resolution","question"]`
  - `Claim(kind: ClaimKind, about_surface: list[str], text_span: tuple[int, int], refers_to_claim_idx: int | None)`
  - `MessageExtraction(label: ThreadLabel, claims: list[Claim])`
  - `extract_chat(slack_df, resolver, llm) -> tuple[DataFrame, DataFrame]`. The first is `messages_out[raw_id, channel, ts_utc, user, thread_ts, category, sentiment, is_incident_related, status]` with `status` in `ok unclassified`. The second is `claims_out[raw_id, claim_idx, kind, about_app_uri, about_surface, span_start, span_end, refers_to]`.
  - On `LLMContractError`, it writes `category="unclassified"`, `status="unclassified"` and a quarantine row with reason `llm_contract`. The message row itself is never dropped.
  - Edits keep both versions (`edited_from_raw_id`). Retractions point at the claim they retract.

- [ ] **Step 1: Write the failing tests**

```python
def test_out_of_scheme_category_rejected(slack_fixture, resolver):
    fake = FakeClient({ANY_KEY: LLMContractError("category 'outage' not allowed")})
    msgs, _ = extract_chat(slack_fixture, resolver, fake)
    assert set(msgs["status"]) == {"unclassified"} and msgs.height == slack_fixture.height

def test_retraction_links_to_hypothesis(slack_fixture, resolver, scripted_llm):
    _, claims = extract_chat(slack_fixture, resolver, scripted_llm)
    r = claims.filter(pl.col("kind") == "retraction").row(0, named=True)
    assert claims.filter(pl.col("claim_idx") == r["refers_to"])["kind"][0] == "hypothesis"

def test_text_span_is_inside_message(slack_fixture, resolver, scripted_llm):
    _, claims = extract_chat(slack_fixture, resolver, scripted_llm)
    assert all(0 <= s < e <= msg_len(slack_fixture, rid) for rid, s, e in claims.select("raw_id","span_start","span_end").iter_rows())

@pytest.mark.live
def test_live_glm_labels_hero_channel(preprocessed_hero_raw):
    msgs, claims = extract_chat(hero_slack(preprocessed_hero_raw), resolver(), FireworksClient())
    assert (msgs["status"] == "ok").mean() >= 0.95
    assert claims.filter((pl.col("kind") == "mitigation") & (pl.col("about_app_uri") == app_uri("title-metadata-svc"))).height >= 1
```

- [ ] **Step 2: Run them.** Expected FAIL.
- [ ] **Step 3: Implement.** The system prompt lists the allowed values verbatim. Batch the messages of each thread together in one call to cut cost.
- [ ] **Step 4: Run them.** Expected PASS. Then run the live test once with `uv run pytest -m live tests/preprocess/test_chat_extract.py` and paste the pass rate into the commit message.
- [ ] **Step 5: Commit.** `feat: closed-output chat extraction`

### Task 10: Reachability ledger across the whole preprocess output

**Files:**
- Modify: `src/e2ekg/preprocess/ledger.py`
- Test: `tests/preprocess/test_ledger_reachability.py`

**Interfaces:**
- Produces `check_reachability(proc_root, fingerprint_tables: dict[str, DataFrame]) -> ReachabilityReport(rows_total: int, rows_reachable: int, unreachable_raw_ids: list[str])`. A row is reachable when a selector in some fingerprint table selects it, or when it is an event, alert, incident, catalog or chat row (these become nodes directly in Task 12).
- `e2ekg ledger --scenario hero` prints both reports and exits non-zero on any gap.

- [ ] **Step 1: Write the failing test**

```python
def test_every_parsed_row_reachable(preprocessed_hero):
    r = check_reachability(PROC/"hero", fingerprints(preprocessed_hero))
    assert r.unreachable_raw_ids == [] and r.rows_reachable == r.rows_total

def test_ledger_cli_fails_on_deleted_row(tmp_copy_of_processed_hero):
    drop_one_log_row(tmp_copy_of_processed_hero)
    assert run_cli("ledger", "--proc", tmp_copy_of_processed_hero).returncode != 0
```

- [ ] **Step 2: Run them.** Expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected PASS. Then run `uv run e2ekg ledger --scenario all` and keep the output.
- [ ] **Step 5: Commit.** `feat: reachability ledger`

### Task 11: Core ontology, SKOS schemes, SHACL shapes, competency questions

**Files:**
- Create: `ontology/core.ttl`, `ontology/schemes/{error_category,fault_kind,change_kind,severity,message_category,sentiment,claim_kind,signal_type}.ttl`, `ontology/shapes.ttl`, `ontology/cq/01-bedrock-incidents.rq` through `ontology/cq/15-*.rq` with `.expected.json`, `src/e2ekg/graph/uris.py`, `src/e2ekg/graph/shacl.py`
- Test: `tests/ontology/test_ontology.py`, `tests/ontology/test_cq.py`

**Interfaces:**
- Produces in `uris.py`: `NS: dict[str, rdflib.Namespace]` for the 12 namespaces plus `prov skos owl time xsd`. `uri(ns: str, kind: str, stable_id: str) -> URIRef`. `app_uri(app_id) -> URIRef` gives `https://kg.local/app/application/<id>`.
- `core.ttl` classes are exactly the spec's section 5 list. Key properties: `ops:ownedBy`, `ops:deployedIn`, `ops:dependsOn`, `ops:affects`, `ops:severity`, `ops:changedApp`, `ops:atTime`, `ops:windowStart`, `ops:windowEnd`, `ops:onApplication`, `ops:rawSelector`, `ops:count`, `ops:magnitude`, `ops:confidence`, `ops:hasLink`, `ops:linkOrder`, `ops:cites`, `ops:resolvedBy`, `ops:similarTo`, `chat:category`, `chat:sentiment`, `chat:isIncidentRelated`, `chat:claimKind`, `chat:about`, `chat:retracts`. `ops:ownedBy` points at an `ops:Team` node with an `rdfs:label` (the post's `"Team Bedrock"` literal becomes the label).
- `shapes.ttl` encodes the Global Constraints invariants:
  - Every subject has `rdf:type`.
  - `ops:Application` has `ops:inCatalog true`.
  - Every instance of `ops:Signal` and its subclasses has an `ops:rawSelector` or a `prov:wasDerivedFrom`.
  - `chat:category` is in the message category scheme.
  - `ops:dependsOn` appears at most 50 times per node.
  - `ops:Finding` has `ops:onApplication`, `ops:cites` (at least 1) and `ops:confidence` in [0, 1].
- `validate(graph_or_store) -> tuple[bool, str]`.
- The CQ runner loads `core.ttl` plus the store, runs each `.rq`, and compares the result to `.expected.json` as a set of rows. Required CQs:
  - 01 is the post's question: "Which incidents affected applications owned by Team Bedrock in us-east-1?"
  - 02 to 07 have expected rows taken from the hero answer key:
    - 02. Changes in the downstream closure of `homepage-graphql` in the 40 min before 15:20.
    - 03. Apps with a log spike in the window.
    - 04. Who resolved a similar incident last quarter.
    - 05. The first mitigation claim about `title-metadata-svc`.
    - 06. Retracted hypotheses.
    - 07. Owner team of every app on the causal chain.

- [ ] **Step 1: Write the failing tests**

```python
def test_ontology_parses_and_is_consistent():
    g = load_ontology()
    assert (NS["ops"].Application, RDF.type, OWL.Class) in g
    assert not list(g.subjects(RDFS.subClassOf, NS["ops"].Service))   # no generic Service type

def test_shapes_reject_untyped_node():
    ok, report = validate(graph_from("ex:x ops:affects ex:y ."))
    assert not ok and "rdf:type" in report

def test_shapes_reject_bad_category():
    ok, _ = validate(graph_from('ex:t a chat:Thread ; chat:category "outage" .'))
    assert not ok

@pytest.mark.parametrize("cq", sorted(CQ_DIR.glob("*.rq")))
def test_cq(cq, harvested_hero_store):          # fixture built in Task 12
    assert run_cq(harvested_hero_store, cq) == expected(cq)
```

- [ ] **Step 2: Run them.** The first three FAIL. `test_cq` is collected and errors on the missing fixture. That is expected until Task 12.
- [ ] **Step 3: Write the Turtle files and the runner.**
- [ ] **Step 4: Run the first three.** Expected PASS.
- [ ] **Step 5: Commit.** `feat: core ontology, schemes, shapes, competency questions`

### Task 12: Store, enricher framework and harvest

**Files:**
- Create: `src/e2ekg/graph/store.py`, `src/e2ekg/graph/enricher.py`, `src/e2ekg/graph/enrichers/{catalog,topology,changes,metrics,logs,traces,alerts_incidents,chat,history}.py`, `src/e2ekg/graph/harvest.py`
- Test: `tests/graph/test_store.py`, `tests/graph/test_enrichers.py`, `tests/graph/test_harvest.py`

**Interfaces:**
- Produces:
  - `Store(path: Path)` over `pyoxigraph.Store`, with `.replace_graph(name: URIRef, triples: Iterable[Triple]) -> int`, `.query(sparql: str) -> list[dict]`, `.dump_turtle(out: Path)` (sorted, deterministic) and `.quad_count() -> int`.
  - `EnrichmentResult(success: bool, triples_added: int, error: str | None)`
  - `class Enricher(ABC)`. `name: str`. `reads: tuple[str, ...]`. `fetch(self, ctx: HarvestContext) -> Any`. `analyze(self, raw) -> Any`. `emit(self, result) -> Iterable[Triple]`. `run(self, ctx) -> EnrichmentResult` calls them in order, replaces graph `https://kg.local/graph/{name}/{scenario}`, and catches exceptions into `success=False` (best effort).
  - `HarvestContext(scenario: str, proc_root: Path, store: Store, resolver: Resolver, llm: LLMClient)`
  - `ENRICHERS: tuple[type[Enricher], ...]` in this exact order: Catalog, Topology, Changes, MetricAnomaly, LogPattern, TraceFingerprint, AlertsIncidents, Chat, History. Correlation is appended in Task 13.
  - `harvest(ctx) -> HarvestReport(results: dict[str, EnrichmentResult], shacl_ok: bool, quads: int)`. It runs sequentially, then SHACL, then `dump_turtle(data/graph/<scenario>/harvest.ttl)`.
  - Topology merges catalog edges with trace edge fingerprints and caps them at 50 per direction by call volume, recording `ops:droppedEdges n` on the node.
  - History emits `ops:Pattern` nodes with `ops:support` and `ops:frequency`, for example `(Deployment@api-gateway, SEV2)` with frequency 0.4.
- CLI: `e2ekg harvest --scenario hero`.

- [ ] **Step 1: Write the failing tests**

```python
def test_harvest_twice_is_identical(tmp_path, preprocessed_hero):
    a = harvest(ctx(tmp_path/"a")); b = harvest(ctx(tmp_path/"a"))
    assert a.quads == b.quads and turtle(tmp_path/"a") == turtle_after_second_run(tmp_path/"a")

def test_shacl_clean(harvested_hero):
    assert harvested_hero.report.shacl_ok

def test_failed_enricher_degrades_not_blocks(tmp_path, preprocessed_hero, monkeypatch):
    monkeypatch.setattr(ChatEnricher, "fetch", boom)
    r = harvest(ctx(tmp_path))
    assert r.results["chat"].success is False and r.results["history"].success is True

def test_all_twelve_namespaces_used(harvested_hero):
    assert namespaces_in_use(harvested_hero.store) >= set(NS12)

def test_cq_suite_now_passes(harvested_hero_store):
    for cq in sorted(CQ_DIR.glob("*.rq")):
        assert run_cq(harvested_hero_store, cq) == expected(cq), cq.name

def test_raw_evidence_reachable_from_graph(harvested_hero):
    for rid in load_answers("hero")["evidence_raw_ids"]:
        assert graph_reaches_raw_id(harvested_hero.store, PROC/"hero", rid)
```

- [ ] **Step 2: Run them.** Expected FAIL.
- [ ] **Step 3: Implement.** `harvested_hero` and `harvested_hero_store` are session fixtures in `tests/conftest.py`. They use a `FakeClient` replaying the recorded responses from Task 9's live run (`tests/fixtures/llm_cache/`).
- [ ] **Step 4: Run them, and also** `uv run pytest tests/ontology -v`. Expected all PASS, including Task 11's CQs.
- [ ] **Step 5: Commit.** `feat: oxigraph store, ordered enrichers, idempotent harvest`

### Task 13: Correlation engine

**Files:**
- Create: `src/e2ekg/correlate/engine.py`, `src/e2ekg/graph/enrichers/correlation.py`
- Modify: `src/e2ekg/graph/enricher.py` (append `CorrelationEnricher` to `ENRICHERS`)
- Test: `tests/correlate/test_engine.py`

**Interfaces:**
- Produces:
  - `Signal(uri: str, type: str, app: AppId | None, at_utc: datetime, severity: float)`, with `type` among the 13 signal types.
  - `PRIORS: dict[tuple[str, str], float]` with these values (mine, since the post gives none). `("deployment","log_error")=0.9`, `("config_change","metric_anomaly")=0.9`, `("config_change","autoscaling")=0.95`, `("autoscaling","metric_anomaly")=0.85`, `("metric_anomaly","log_error")=0.75`, `("log_error","slow_trace")=0.75`, `("slow_trace","metric_anomaly")=0.75`, `("failed_trace","incident")=0.85`, `("metric_anomaly","alert")=0.9`, `("alert","incident")=0.95`. The default for any other pair is `0.5`.
  - `pair_score(a, b, prior) = prior * (1 - 0.4 * dt/600) * (0.5 + 0.5 * b.severity)`, for `0 < dt <= 600`.
  - Topology gate. A pair `(a, b)` is allowed only if `a.app == b.app`, or `b.app` calls `a.app` through edges (cause downstream, effect upstream), or `b.type in {"alert","incident"}`.
  - `correlate(signals, edges, symptom_uri, gate: bool = True, priors=PRIORS) -> list[Chain]`. Chains are paths that end at the symptom, each found by a backward beam search (width 20, max length 10). `Chain(links: tuple[Signal, ...], confidence: float)` takes the geometric mean of its pair scores. Chains with confidence `< 0.6` are dropped. Results are sorted by confidence, then by earliest root.
  - `CorrelationEnricher` emits `ops:CausalChain` nodes with `ops:hasLink` and `ops:linkOrder`, `ops:confidence`, and `prov:wasDerivedFrom` to each link node.
  - `History` patterns adjust priors through `priors_with_history(store) -> dict`, which Task 18 uses.

- [ ] **Step 1: Write the failing tests**

```python
def test_true_chain_ranks_first(harvested_hero_signals):
    chains = correlate(*harvested_hero_signals, gate=True)
    assert chains[0].links[0].uri == change_uri("chg-cfg-0001")
    assert chains[0].confidence >= 0.6

def test_red_herrings_not_top_with_gate(harvested_hero_signals):
    roots = [c.links[0].uri for c in correlate(*harvested_hero_signals, gate=True)[:3]]
    assert change_uri("chg-dep-0001") not in roots and change_uri("chg-rel-0001") not in roots

def test_without_gate_api_gateway_trap_survives(harvested_hero_signals):
    roots = [c.links[0].uri for c in correlate(*harvested_hero_signals, gate=False)]
    assert change_uri("chg-dep-0001") in roots

def test_skewed_host_does_not_reorder_chain(harvested_hero_signals):
    links = [l.type for l in correlate(*harvested_hero_signals)[0].links]
    assert links.index("metric_anomaly") < links.index("log_error")

def test_control_variant_yields_no_chain_to_a_change(harvested_control_signals):
    assert all(c.links[0].type not in CHANGE_TYPES for c in correlate(*harvested_control_signals))
```

- [ ] **Step 2: Run them.** Expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, then run** `e2ekg harvest --scenario all` and check `ops:CausalChain` counts per scenario.
- [ ] **Step 5: Commit.** `feat: topology-gated correlation engine`

### Task 14: Variant scenarios end to end through the graph

**Files:**
- Test: `tests/test_variants_e2e.py`

**Interfaces:**
- Consumes: everything through Task 13.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.parametrize("name", ["v-bad-deploy","v-cache-storm","v-hot-partition","v-cert-expiry","v-retry-storm"])
def test_variant_root_cause_top1(name, harvested):
    s = harvested(name)
    assert correlate(*s.signals)[0].links[0].uri == change_uri(s.answer["root_cause_change_id"])

@pytest.mark.parametrize("name", ALL_SCENARIOS)
def test_ledgers_and_shacl(name, harvested):
    s = harvested(name)
    assert s.coverage.gaps == [] and s.reach.unreachable_raw_ids == [] and s.report.shacl_ok
```

- [ ] **Step 2: Run it.** Fix the renderers or the engine until it passes. Never edit the answer key to fit.
- [ ] **Step 3: Commit.** `test: variants pass end to end through graph`

### Task 15: Ontology evolution proposals

**Files:**
- Create: `src/e2ekg/ontology_evolve/propose.py`
- Test: `tests/ontology_evolve/test_propose.py`

**Interfaces:**
- Produces:
  - `mine_candidates(proc_root, store) -> Candidates`. It finds templates whose error words aren't covered by an `error_category` concept, metric tag keys absent from the core properties, and event types absent from `change_kind`.
  - `OntologyProposal(turtle: str, rationale: str, examples: list[str])`
  - `propose(candidates, llm) -> list[OntologyProposal]`, written to `ontology/proposals/NNN.ttl` with the rationale as a Turtle comment header.
  - `check_proposal(path) -> tuple[bool, str]`. It loads core plus extensions plus the proposal, then runs SHACL and the CQ suite against `harvested_hero_store`.
  - Approval is manual. You move the file to `ontology/extensions/`. `load_ontology()` loads `extensions/*.ttl`.
- CLI: `e2ekg ontology propose` and `e2ekg ontology check ontology/proposals/NNN.ttl`.

- [ ] **Step 1: Write the failing tests**

```python
def test_proposal_breaking_cq_is_rejected(tmp_path):
    p = write(tmp_path/"001.ttl", "ops:Application owl:equivalentClass ops:Team .")
    ok, report = check_proposal(p)
    assert not ok

def test_proposal_adding_concept_passes(tmp_path):
    p = write(tmp_path/"002.ttl", NEW_ERROR_CONCEPT_TTL)
    assert check_proposal(p)[0]

def test_candidates_include_unmapped_template(preprocessed_hero, harvested_hero_store):
    assert "connection pool" in " ".join(mine_candidates(PROC/"hero", harvested_hero_store).error_templates).lower()
```

- [ ] **Step 2: Run them.** Expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected PASS.
- [ ] **Step 5: Commit.** `feat: LLM-proposed, test-gated ontology extensions`

### Task 16: Agent tools over the graph

**Files:**
- Create: `src/e2ekg/agent/tools.py`, `src/e2ekg/agent/report.py`
- Test: `tests/agent/test_tools.py`

**Interfaces:**
- Produces in `report.py`:
  - `FindingIn(about_app: AppId, claim: str, signal_uris: list[str], confidence: float)`
  - `Hypothesis(root_cause_uri: str, chain: list[str], confidence: float, evidence: list[str], refuted_alternatives: list[str])`
  - `RCAReport(verdict: Literal["root_cause_found","inconclusive"], hypotheses: list[Hypothesis], summary: str)`
- Produces in `tools.py`: `make_tools(store: Store, proc_root: Path, scenario: str, specialist: str) -> dict[str, BaseTool]`. Each tool is a LangChain `@tool` with typed args, returns compact JSON at most 4,000 characters long, and sets `LIMIT 200` in every query:
  - `find_symptom(alert_id: str)`
  - `dependency_closure(app: str, direction: Literal["down","up"])`
  - `changes_in_window(apps: list[str], start: str, end: str)`
  - `anomalies_in_window(apps, start, end)`
  - `log_spikes(apps, start, end)`
  - `trace_edges(apps, start, end)`
  - `alerts_incidents(start, end)`
  - `similar_incidents(app: str, kinds: list[str])`
  - `chat_claims(channel: str, kinds: list[str])`
  - `causal_chains(symptom_uri: str)`
  - `raw_evidence(uri_or_selector: str, limit: int = 20)` runs DuckDB over `proc_root` and returns the raw lines with their raw_ids.
  - `sparql_select(query: str)` rejects anything but `SELECT` and forces a `LIMIT`.
  - `record_finding(f: FindingIn)` writes an `ops:Finding` into graph `https://kg.local/graph/findings/{scenario}/{specialist}`. SHACL checks it before the write commits, and it returns the error text on failure.
  - `read_findings(app: str | None)`
  - The input `app` strings go through `Resolver`, so the agent may say `TMS`.

- [ ] **Step 1: Write the failing tests**

```python
def test_changes_in_window_finds_root_and_herrings(tools):
    closure = [a["id"] for a in json.loads(tools["dependency_closure"].invoke({"app": "homepage-graphql", "direction": "down"}))]
    out = json.loads(tools["changes_in_window"].invoke({"apps": closure, "start": "2026-10-14T21:40Z", "end": "2026-10-14T22:20Z"}))
    assert {"chg-cfg-0001","chg-exp-0001"} <= {c["id"] for c in out}

def test_raw_evidence_returns_original_bytes(tools):
    out = json.loads(tools["raw_evidence"].invoke({"uri_or_selector": spike_uri("title-batch-loader")}))
    assert all(raw_bytes(r["raw_id"]) == r["line"].encode() for r in out)

def test_sparql_select_blocks_updates(tools):
    assert "only SELECT" in tools["sparql_select"].invoke({"query": "DELETE WHERE { ?s ?p ?o }"})

def test_record_finding_rejects_without_evidence(tools):
    assert "ops:cites" in tools["record_finding"].invoke({"f": {"about_app": "row-ranker", "claim": "x", "signal_uris": [], "confidence": 0.5}})

def test_nickname_input_resolves(tools):
    assert json.loads(tools["dependency_closure"].invoke({"app": "TMS", "direction": "up"}))
```

- [ ] **Step 2: Run them.** Expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected PASS.
- [ ] **Step 5: Commit.** `feat: typed graph tools and finding contract`

### Task 17: DeepAgents AutoRCA with seven specialists

**Files:**
- Create: `src/e2ekg/agent/specialists.py`, `src/e2ekg/agent/autorca.py`, `src/e2ekg/agent/prompts/*.md`
- Test: `tests/agent/test_autorca.py`

**Interfaces:**
- Consumes: `make_tools`, `RCAReport`, `MODEL_ID`.
- Produces:
  - `SPECIALISTS: dict[str, list[str]]`, mapping each specialist to its tool names. `topology` gets dependency_closure and trace_edges. `change` gets changes_in_window. `metrics` gets anomalies_in_window. `logs` gets log_spikes and raw_evidence. `traces` gets trace_edges and raw_evidence. `alerts_incidents` gets find_symptom and alerts_incidents. `history` gets similar_incidents and chat_claims. Every specialist also gets `record_finding`.
  - `build_specialists(store, proc_root, scenario) -> list[SubAgent]`. Each is a dict with `name`, `description`, `system_prompt` from `prompts/<name>.md`, `tools`, `response_format=None`. Each prompt ends with "Report only through record_finding. Return one line naming the finding URIs."
  - `build_autorca(store, proc_root, scenario, model: str | BaseChatModel = f"fireworks:{MODEL_ID}") -> CompiledGraph`. It wraps `create_deep_agent(model=..., tools=[find_symptom, causal_chains, read_findings, raw_evidence, sparql_select], system_prompt=prompts/autorca.md, subagents=..., response_format=RCAReport, backend=CompositeBackend(default=StateBackend(), routes={"/evidence/": FilesystemBackend(root_dir=<abs data/agent/scenario>, virtual_mode=True)}))`.
  - The orchestrator prompt requires these steps:
    1. Dispatch all seven specialists.
    2. Read the findings.
    3. Read the correlation chains.
    4. For each candidate, run a refutation check (is it in the dependency closure, does its timing precede the symptom on skew-corrected time, does it explain all four clients).
    5. Return `RCAReport`. If no candidate survives refutation, the verdict is `inconclusive`.
  - `run_rca(alert_id: str, scenario: str) -> RCAReport`
- CLI: `e2ekg rca --scenario hero --alert <id>`.

- [ ] **Step 1: Write the failing tests**

```python
def test_agent_builds_with_seven_specialists(harvested_hero):
    agent = build_autorca(harvested_hero.store, PROC/"hero", "hero", model=fake_tool_model())
    assert set(subagent_names(agent)) == set(SPECIALISTS) and len(SPECIALISTS) == 7

def test_specialist_tool_isolation(harvested_hero):
    subs = {s["name"]: s for s in build_specialists(harvested_hero.store, PROC/"hero", "hero")}
    assert tool_names(subs["logs"]) == {"log_spikes","raw_evidence","record_finding"}

@pytest.mark.live
def test_live_hero_rca():
    r = run_rca(hero_alert_id(), "hero")
    assert r.verdict == "root_cause_found"
    assert r.hypotheses[0].root_cause_uri == change_uri("chg-cfg-0001")
    assert set(r.hypotheses[0].evidence) & set(evidence_uris("hero"))
```

- [ ] **Step 2: Run them.** Expected FAIL.
- [ ] **Step 3: Implement.** If GLM-5.3-flash fails structured output through `response_format`, switch to `ToolStrategy(RCAReport)` and note it in the commit.
- [ ] **Step 4: Run the unit tests (PASS), then the live test once.** Record pass or fail and token cost in the commit message. A live failure here doesn't block Task 18. Task 18 measures it properly.
- [ ] **Step 5: Commit.** `feat: DeepAgents AutoRCA with graph-as-interface specialists`

### Task 18: Evals, ablations and the flywheel

**Files:**
- Create: `src/e2ekg/evals/run.py`, `src/e2ekg/evals/score.py`, `src/e2ekg/flywheel/adapt.py`
- Test: `tests/evals/test_score.py`, `tests/flywheel/test_adapt.py`

**Interfaces:**
- Produces:
  - `score(report: RCAReport, answers: dict) -> Score(top1: bool, top3: bool, evidence_precision: float, trap: bool, control_false_positive: bool)`. `trap` is true when the top-1 root is a red herring. `control_false_positive` is true when the control scenario has verdict `root_cause_found`.
  - `run_evals(scenarios: list[str], runs: int, arms: list[Literal["graph","raw_only","no_gate"]]) -> pl.DataFrame`, written to `data/evals/<timestamp>.parquet` plus a markdown summary.
    - The `raw_only` arm is the same orchestrator with tools replaced by a `FilesystemBackend` over `data/raw/<scenario>` and no graph tools.
    - The `no_gate` arm uses `causal_chains` computed with `gate=False`.
  - `adapt(store, eval_df) -> list[URIRef]`. Confirmed chains (top-1 correct with human approval recorded in `data/evals/approvals.jsonl`) become `ops:Pattern` nodes. `priors_with_history` reads them.
- CLI: `e2ekg eval --scenarios all --runs 3 --arms graph,raw_only,no_gate`.

- [ ] **Step 1: Write the failing tests**

```python
def test_score_trap():
    assert score(report_with_root("chg-dep-0001"), hero_answers()).trap

def test_score_control_false_positive():
    assert score(report_found("chg-x"), control_answers()).control_false_positive

def test_adapt_does_not_flip_hero(harvested_hero_signals, store_with_patterns):
    pri = priors_with_history(store_with_patterns)
    assert correlate(*harvested_hero_signals, priors=pri)[0].links[0].uri == change_uri("chg-cfg-0001")
```

- [ ] **Step 2: Run them.** Expected FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected PASS.
- [ ] **Step 5: Live run.** `uv run e2ekg eval --scenarios all --runs 3 --arms graph,raw_only,no_gate`. Keep the summary table. The program passes when, on the `graph` arm, top1 is at least 6 of the 7 scenarios on majority vote, trap rate is 0 on hero, and the control has no false positive. The graph arm must also beat `raw_only` on top1.
- [ ] **Step 6: Commit.** `feat: evals, ablations, flywheel adapt`

---

## Spec coverage

| DESIGN.md section | Tasks |
| --- | --- |
| 2 Data shape, hero, history, variants | 1, 2, 3 |
| 3 Messiness registry, calibration, sizes | 4, 5 |
| 4 Lossless contract, entropy ladder, steps | 6, 7, 8, 9, 10 |
| 5 CQs, core, SKOS, expansion, TBox/ABox split | 11, 15 |
| 6 Oxigraph, enrichers, invariants, caps, Turtle, correlation, gating | 12, 13, 14 |
| 7 Agent, seven specialists, graph as interface, evals, ablations | 16, 17, 18 |
| 8 Flywheel | 18 |
