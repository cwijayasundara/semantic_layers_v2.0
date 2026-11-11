# e2ekg — Ontology-Driven Observability: Design & Architecture

**End-to-end knowledge graph for root-cause analysis.** This repository rebuilds a
realistic, working version of the system described in the Netflix TechBlog post
*"Ontology-Driven Observability: Building an End-to-End Knowledge Graph at Netflix
Scale"* (see `docs/netflix_post.md`). It covers the full arc of that post:

- generate **messy MELT data** (Metrics, Events, Logs, Traces) that hides one true
  root cause among planted red herrings,
- **preprocess** it without losing a single byte,
- **derive and grow an ontology** (RDF + OWL + SKOS + SHACL) from it,
- **harvest** the data into an RDF knowledge graph through ordered enrichers,
- run a **topology-gated correlation engine** over the graph,
- let an **agentic system (AutoRCA)** investigate a live alert over the graph,
- and close the loop with a **knowledge flywheel** and an **evaluation harness**.

Everything is deterministic and reproducible from a seed; every stochastic component
(LLM calls) is constrained to closed output contracts so the end result — the graph
— answers the same SPARQL query the same way every time. That is the post's central
claim ("creating determinism from non-determinism"), implemented and tested here.

---

## 1. System overview

```
+-------------------------------------------------------------------------------------------------+
|                                 e2ekg end-to-end pipeline                                        |
+-------------------------------------------------------------------------------------------------+
|                                                                                                   |
|  [1] GENERATE                    [2] PREPROCESS                        [3] HARVEST (RDF)           |
|  Scenario model                  Lossless parse + normalize            Ordered enrichers          |
|  (seeded, deterministic)    -->  + entropy ladder L1..L5        -->    into named graphs           |
|  mess injectors (noise)          Parquet + quarantine                  Oxigraph quad store        |
|  answer key (held out)           conservation ledger                   SHACL-validated            |
|  raw tree (never modified)       clock-skew correction                 sorted Turtle dump         |
|                                                                                                   |
|  data/raw/<scenario>/            data/processed/<scenario>/            data/graph/<scenario>/     |
|                                                                                                   |
|                                        |                                   |                      |
|                                        v                                   v                      |
|                             [4] ONTOLOGY (TBox + schemes)      [5] CORRELATION ENGINE                |
|                             core.ttl, SKOS schemes             13 signal types, prior weights        |
|                             shapes.ttl (SHACL)                 topology gate + explaining away       |
|                             competency questions               -> ops:CausalChain (conf >= 0.6)      |
|                             LLM-proposed extensions (gated)                                          |
|                                                                                                   |
|                                                                                |                  |
|                                                                                v                  |
|  [8] SERVING (API + UI)                    [6] AGENTIC SYSTEM (AutoRCA)                        |
|  FastAPI over the graph tools               DeepAgents commander + 7 specialists               |
|  Next.js console: scenarios,    <--         typed graph tools, raw_evidence                     |
|  case graph, ontology, SPARQL,              findings recorded AS triples                        |
|  evals                                      ranked RCAReport w/ evidence                       |
|                                                         |                                       |
|                                                         v                                       |
|                                              [7] KNOWLEDGE FLYWHEEL                             |
|                                              Observe -> Enrich -> Infer -> Adapt                |
|                                              human-approved roots become ops:Patterns           |
|                                              (priors for the next harvest)                      |
+-------------------------------------------------------------------------------------------------+
```

**Ground rule of the repo:** pipeline code never reads the answer key
(`data/raw/_answers/`), and the answer key is only ever read by the *scorer*.
The scenario is the truth; the pipeline must rediscover it from the noisy signals.

---

## 2. Repository layout

```
semantic_layers_v2.0/
├── ontology/                      # Ontology as code (TBox, in git, reviewed by PR)
│   ├── core.ttl                   #   OWL classes + properties (the "shared agreement")
│   ├── shapes.ttl                 #   SHACL shapes validated after every harvest
│   ├── schemes/                   #   Closed SKOS taxonomies (LLM output contracts)
│   │   ├── change_kind.ttl  claim_kind.ttl  error_category.ttl  fault_kind.ttl
│   │   ├── message_category.ttl  sentiment.ttl  severity.ttl  signal_type.ttl
│   ├── extensions/                #   Approved data-driven extensions (proposal-001..004)
│   ├── proposals/                 #   LLM-proposed extensions awaiting human approval
│   └── cq/                        #   15 competency questions: <n>.rq + .expected.json
├── src/e2ekg/
│   ├── scenario/                  # [1] Typed scenario model, hero + variants, propagation
│   ├── generate/                  # [1] Renderers (metrics/logs/traces/events/chat/catalog)
│   │   └── mess/                  #     Registered mess injectors (identity, units, skew…)
│   ├── preprocess/                # [2] Parsers, timefix, identity, fingerprints, ledger,
│   │   ├── parsers/               #     as-of views, chat extraction (closed-output LLM)
│   │   └── …                      #     csv_events gitlog log4j logfmt nginx otlp prom
│   │                              #     properties slack yaml_catalog zipkin json_lines
│   ├── graph/                     # [4] Store (Oxigraph), enricher framework, 10 enrichers,
│   │   ├── enrichers/             #     SHACL validation, URI scheme, harvest driver
│   ├── correlate/engine.py        # [5] Topology-gated causal-chain search
│   ├── ontology_evolve/propose.py # [3] Mine candidates -> LLM proposals -> gated
│   ├── agent/                     # [6] AutoRCA: commander, 7 specialists, typed tools,
│   │   ├── prompts/               #     findings, evidence drill-down, RCAReport
│   └── api/app.py                 # [8] FastAPI service (read endpoints + RCA jobs)
│   ├── flywheel/adapt.py          # [7] Approvals -> ops:Pattern
│   └── evals/                     #     Arms, runner, scorer, summary
├── ui/                            # [8] Next.js console (scenarios, case, ontology, SPARQL, evals)
├── scripts/
│   ├── ingest.sh                  #   One-shot: generate -> preprocess -> ledger -> harvest
│   ├── start-backend.sh           #   uvicorn e2ekg.api.app:app
│   └── start-ui.sh                #   next dev
├── data/                          # raw/ processed/ graph/ evals/ (generated; not in git)
├── docs/DESIGN.md                 #   Compact design brief (the doc this expands)
└── docs/netflix_post.md           #   The Netflix post this system rebuilds
```

**Stack.** Python 3.12 (uv), polars + DuckDB + Parquet (columnar processing),
drain3 (log templating), pyoxigraph (Rust quad store, SPARQL 1.1), rdflib, pyshacl,
rapidfuzz, DeepAgents + langchain-fireworks (agent, GLM-5.3-flash on Fireworks),
FastAPI + Next.js (serving). Pydantic models everywhere at every boundary.

---

## 3. Layer 1 — How the MELT data is created

### 3.1 The scenario is the single source of truth

Everything hangs off one typed, frozen **`Scenario`** model
(`src/e2ekg/scenario/model.py`). The generator renders it into raw signals; the
pipeline must recover its `answer_key` from the noise.

```
                       SCENARIO (frozen pydantic model)
  +-------------------------------------------------------------------------+
  |  catalog     : Application[]      id, aliases[], owner team, tier,      |
  |                                    region[], kind (client|edge|service|  |
  |                                    datastore|cache)                     |
  |  topology    : DependencyEdge[]   caller -> callee, protocol, pool size |
  |  window      : Window             start, end, metric step               |
  |  fault       : Fault              kind, target app, start, trigger      |
  |                                    change, fix change                    |
  |  changes     : Change[]           id, kind, app, time, is_root_cause    |
  |  history     : PastIncident[]     ~30 incidents over 90 days, each with |
  |                                    its own alert, record and Slack chan. |
  |  answer_key  : AnswerKey          root_cause_change_id, causal_chain,   |
  |                                    red_herring_change_ids, evidence_tags |
  +-------------------------------------------------------------------------+
            |                                       |
            v                                       v
     renderers (generate/)                  held out in data/raw/_answers/
     raw MELT signals                       <name>.json + .manifest.json
     (NEVER see the answer key)             (NEVER seen by the pipeline)
```

Fault kinds: `capacity_reduction`, `bad_deploy`, `cache_eviction_storm`,
`db_hot_partition`, `cert_expiry`, `retry_storm` (plus `none` for control
scenarios). Change kinds: `deployment`, `config_change`, `scaling`,
`experiment_allocation`, `commit`, `client_release`.

### 3.2 The hero scenario (mirrors the post's opening incident)

- **Six-hop topology:** clients (tv, android, ios, web) -> `edge-gateway` ->
  `homepage-graphql` (federated) -> `lolomo-svc` -> `row-ranker` ->
  `title-metadata-svc`.
- **Root cause:** a config change lowers `title-metadata-svc`'s autoscaler min
  instances from 48 to 12 at T0. Evening traffic rises, upstream connection pools in
  `row-ranker` exhaust, timeouts climb the chain, `homepage-graphql` returns partial
  rows, and LOLOMO time-to-render p90 goes up ~1.5 s — the post's regression.
- **Red herrings**, each engineered to fool a naive method:

  | Red herring | Trap |
  |---|---|
  | Android client release 8 minutes before the onset | temporal proximity |
  | A/B allocation change on a ranking experiment | plausible mechanism |
  | An `api-gateway` deploy | the learned "40% of SEV-2s" prior |
  | Chronic `NullPointerException` in `artwork-svc` | volume (always there) |
  | Edge load shedding | a symptom that looks like a cause |

- **The alert fires at 3:30 pm**, logged by different systems in UTC, PT and epoch ms.
- **A ~200-message, 50-person, 15-service Slack channel** with nicknames ("TMS", "the
  ranker"), typos, wrong guesses later retracted, and a mitigation posted before
  anyone names the cause.
- **90 days of incident history (~30 past incidents)** that (a) manufactures the
  misleading `api-gateway` prior — the past SEV-2s really are caused by its deploys —
  and (b) hides the shortcut: one similar `title-metadata-svc` capacity incident last
  quarter, solved by one engineer whose thread names the fix.

Variant scenarios (`v-bad-deploy`, `v-cache-storm`, `v-cert-expiry`, `v-control`,
`v-hot-partition`, `v-retry-storm`) change the fault kind so the system is never
tuned to one story. `v-control` has **no root cause** — a good agent must abstain.

### 3.3 Deterministic fault propagation

Propagation (`scenario/propagation.py`) is computed from the topology, **not**
narrated, so metrics, logs, traces and events stay mutually consistent — that is
what makes the needle findable. No randomness involved:

```
  fault.target degraded at  onset = fault.start + ONSET_DELAY_S[kind]
                                     (capacity_reduction: 780s, bad_deploy: 120s, …)
       |
       |  degradation climbs the CALLER chain, one hop at a time
       v
  depth 1 callers: +HOP_LAG_S[0]   kind: pool_exhaustion (if any edge has a pool)
  depth 2 callers: +HOP_LAG_S[0:2]       timeouts
  depth 3: ...                           partial_response (graphql callers)
  client-facing roots: latency +1.5s     load_shed at roots' gateways +60s later
```

Each degradation is a `(app, kind, start, magnitude, end?)` record the renderers
then paint into their respective signals. If the scenario has a **fix change**
(e.g. "bump min to 40"), recovery is computed the same way
(`RECOVERY_DELAY_S[kind] + hop lags`) and lands strictly *after* the investigation
cutoff.

### 3.4 Renderers and volumetrics

Each family is rendered by a dedicated renderer from the scenario + degradations:

```
 catalog   apps.yaml          (aliases, owners, tiers, regions — the catalog gate)
 metrics   metrics/atlas.jsonl (Atlas-like points)  +  metrics/prom.txt (Prometheus)
 logs      per-service formats: JSON, logfmt, log4j + multi-line stack traces,
           nginx access, syslog; ~2-5M lines at default (laptop) scale
 traces    otlp.jsonl (OTLP JSON) + zipkin.json (legacy v2); ~200k spans/scenario
 events    Spinnaker deploy JSON, fast-property change log, ASG scaling events,
           A/B allocation log, Radar alerts, PagerDuty pages, incident.io record,
           git commits
 chat      Slack export: channels, threads, edits, reactions, emoji, links
```

Volumetrics are calibrated for realism: log templates and their frequency
distribution follow LogHub; metric shapes (diurnal cycle, noise, step changes) and
fault signatures follow the RCAEval RE2 benchmark; span and resource attributes
follow OpenTelemetry semantic conventions. `--scale` multiplies background traffic
on apps **off** the causal path (the needle stays needle-sized).

**Seeded determinism.** One `numpy` generator per renderer, spawned from
`SeedSequence([seed, crc32(scenario.name)])` in a fixed order; the last one is
shared by all mess injectors. Same seed => byte-identical raw tree.

### 3.5 The mess: a registry, not ad-hoc noise

Every kind of mess is a **registered injector** with an id
(`generate/mess/`); every injection is recorded in the manifest. That makes "did
preprocessing undo it without losing data" a testable question.

| Signal | Injected mess |
|---|---|
| **Identity** | Same app as `title-metadata-svc`, `TitleMetadataSvc`, `titlemetadata`, `TMS`, `tms-prod-v042`, ASG name, k8s pod name; application in Atlas, service in Radar, cluster in Spinnaker; stale catalog alias; a rename only Slack remembers |
| **Metrics** | Tag key drift (`nf.app`/`app`/`service`), ms vs s units, counter resets on restart, gaps, late data, duplicate series, high-cardinality (per-instance) tags |
| **Logs** | Per-service formats, mixed timezones and epoch ms, per-host clock skew, truncated lines, at-least-once duplicates, interleaved lines, PII in fields |
| **Traces** | Head sampling (1%) + tail sampling for errors, orphan spans (missing parents), child-before-parent from skew, `service.name` aliases |
| **Events** | Missing fields moved into free-text descriptions, duplicate webhooks |
| **Chat** | Nicknames, typos, threads, edits, retractions, dashboard/alert links |

### 3.6 The answer key and the two-render recovery

The generator writes the answer key **outside** the raw tree
(`data/raw/_answers/<name>.json`): answer key, `as_of_utc` cutoff, `external_ids`
mapping raw-visible ids to scenario change ids, and evidence raw ids by tag. The raw
tree itself never contains anything a real deployment would not contain.

For scenarios with a fix, a naive "render with the fix" would change records *before*
the fix too (random draws reshuffle). So the generator renders **twice** and splices:

```
   baseline render (no fix)                     fixed render
   records up to the cutoff                     only records AFTER the cutoff kept
        |                                             |
        +---------------- splice --------------------+
        |
   one file = [baseline records before its first post-cutoff record]
              [fixed records after the cutoff]
              [baseline's remaining pre-cutoff records = "late arrivals"]
```

Result: every record at or before the cutoff is byte-identical to a no-fix run
(checked by `tests/generate/test_recovery.py`), while the system demonstrably
recovers after the fix. Mess is applied consistently across the splice by replaying
per-injector RNG state.

---

## 4. Layer 2 — Preprocessing: entropy ladder L1–L5

"Nothing lost" has a precise meaning. Putting every log line into RDF is the wrong
reading — Netflix explicitly keeps raw high-volume telemetry out of the graph. The
contract here is: immutable raw bytes, lossless parse, both normalized and original
values kept, provenance on every fact, and a conservation ledger that proves it.

The five layers of the entropy ladder (entropy = the share of information still
trapped in unstructured form). Each step is potentially stochastic; the chain is
deterministic:

```
  L1 PARSE      raw bytes -> records with stable ids      (parser registry,
                 or quarantine rows with a reason          nothing dropped)
        |
  L2 NORMALIZE  times to UTC, units, field names          (originals kept next to
                 per-host clock-skew correction            normalized values)
        |
  L3 RESOLVE    surface names -> catalog URIs             (deterministic first,
   IDENTITY     ("TMS", "tms-prod-v042", "the ranker")     then fuzzy, then LLM with
                 unresolved stays a literal, never an app   a CLOSED choice list)
        |
  L4 FINGERPRINT volume -> patterns                       (Drain3 templates, anomaly
                 (raw telemetry NEVER enters the graph)     change points, edge stats)
        |
  L5 INTERPRET  chat & free text -> typed claims          (closed SKOS schemes,
                 category / sentiment / claim kind          pydantic schema, SHACL)
```

### 4.1 L1 — Lossless parse and the immutable raw layer

`preprocess/run.py` streams every file under `data/raw/<scenario>/`:

1. **Format detection** — sniff the first 64 KiB, match against a parser registry
   (`parsers/`: `otlp`, `zipkin`, `prom`, `log4j`, `logfmt`, `json_lines`, `nginx`,
   `csv_events`, `gitlog`, `properties`, `slack`, `yaml_catalog`).
2. **Lossless split** — the parser yields record spans `(offset, length)`; every
   byte of every file lands in exactly one span. Each span gets a stable id:

   ```
   raw_id = source | rel_path | byte offset | length | sha256[:16]
   e.g.   logs|metrics/atlas.jsonl|4096|214|9f2b…
   ```

3. **Every span becomes parsed row(s) or one quarantine row** with a reason
   (`unknown_format`, `unparseable`, `truncated`, `decode_error`, `llm_contract`).
   Fields the parser does not understand go into a `_residual` column. Duplicates
   stay as separate rows (dedupe happens later, explicitly). **Nothing is dropped
   silently.**

### 4.2 L2 — Time normalization and clock skew

- `ts_raw` is kept exactly as written; `ts_utc` is the epoch-microsecond UTC instant.
- Files whose stamps may lack a zone are parsed *after* zone-aware ones so
  `infer_tz` has a reference clock (per-file median of zone-aware stamps).
- **Per-host clock skew** is estimated from trace parent/child pairs and
  request-id log pairs (`timefix.estimate_skew`), then applied in a second streaming
  pass over logs and spans (`skew_ms` column; `end_utc` corrected for spans).
  Originals stay; `clock_skew.json` records the estimates.

### 4.3 L3 — Identity resolution (the catalog gate)

`preprocess/identity.py` implements the post's "model identity before anything
else":

```
   surface form ("TMS", "TitleMetadataSvc", "tms-prod-v042", "the ranker")
        |
        v
   1. deterministic: exact catalog id / alias table, regex for ASG, pod,
      version suffixes                                  -> method=exact, conf=1.0
        |  (miss)
        v
   2. fuzzy match against the catalog (rapidfuzz/difflib) -> method=fuzzy, conf<1
        |  (miss)
        v
   3. LLM, ONLY with the catalog as a closed choice list  -> method=llm, conf<1
        |  (miss)
        v
   stays a Literal. NEVER becomes an ops:Application.
```

Every parsed row gains an `app_uri` column (nullable); the resolver's decisions
(surface form, method, confidence) are kept and summarized in `ledger.json`. There
is **no generic Service type**: the only software identity is `ops:Application`,
gated by the catalog — the post's first hard lesson.

### 4.4 L4 — Fingerprints (volumetrics become patterns)

`preprocess/fingerprints.py` turns volume into patterns while linking every pattern
back to every row it summarizes:

| Step | Method | Output |
|---|---|---|
| Log templating | Drain3 mines templates; each line -> template id + parameters, raw line kept | `logs.parquet` + template columns |
| Log buckets / spikes | per-(app, template, window) counts vs baseline | `fp_log_buckets`, `fp_log_spikes` |
| Metric cleanup | unit normalization, counter-reset handling, gap marking (no interpolation), dedupe | `metrics_clean` |
| Metric anomalies | robust z-score / change-point per cleaned series | `fp_metric_anomalies` |
| Trace repair | rebuild span trees, mark orphans, skew-correct, aggregate per caller→callee edge (p50/p99, error rate) | `fp_edges` |
| Row-key tables | one row per parsed row with the key a selector matches on | `rows_{logs,metrics,spans}` |

A fingerprint carries a **JSON selector** (source, app, key, t_start, t_end) that
resolves, deterministically, to exactly the raw ids it summarizes. Together the
`fp_*` selectors reach **every** parsed row of logs, metrics and spans — that is
the reachability proof.

### 4.5 L5 — Chat extraction (closed-output LLM)

`preprocess/chat_extract.py` is the post's "constrain stochastic tools with a type
system" made concrete. The LLM (scripted rules or live Fireworks GLM) fills a
pydantic schema whose enums **are** the SKOS schemes:

```
   message label:  category  IN {incident, question, discussion, decision}
                   sentiment IN {positive, neutral, negative, urgent}
                   is_incident_related: bool            (verbatim from the post)

   claims:         kind IN {observation, hypothesis, mitigation, root_cause, retraction}
                   about: surface names -> resolved app URIs
                   text_span: exact character offsets (the quote is kept)
                   retraction -> refers_to: the earlier claim it walks back
```

- Channels are chunked (40 messages, time order) so threads stay whole; the RCA
  cutoff **splits chunks** so no label is made by reading anything after it.
- Batch failure -> per-message retries -> `status="unclassified"` + a quarantine
  row (`llm_contract`). **No message is ever dropped.**
- A retraction's link is resolved from the model's answer, else the replied-to
  message's hypothesis, else the thread's earlier hypothesis — and the rule that
  fired is recorded (`link_method`).

### 4.6 The conservation ledger (the gate for everything downstream)

`e2ekg ledger` runs two proofs and fails loudly:

1. **Byte coverage** — re-read the raw bytes; assert every byte is covered by exactly
   one record span, no gaps, no overlaps, and raw count = parsed + quarantined.
2. **Fingerprint reachability** — every parsed row of logs/metrics/spans is
   reachable from the graph, either directly (`ops:rawSelector`) or through a
   fingerprint's selector. Plus table row counts vs `ledger.json`, chat parity
   (every Slack row classified or quarantined, unclassified ≤ 5%).

```
   raw bytes  ==  sum(record spans)  ==  parsed rows + quarantine rows
   graph  ==>  fingerprints  ==>  every raw row        (no orphans, no phantoms)
```

### 4.7 As-of views (time-travel reads)

`preprocess/asof.py` defines the one read path the harvest takes into a table:
`scan(proc, name, as_of)`. With a cutoff, the table is **as it was observable then**
— rows stamped before the cutoff, fingerprints **recomputed** over those rows with
the same code, and log templates **re-mined** (Drain clusters lines, so a future
line could reshape the template of a past line — templates are re-mined as-of).
Claims exist only when their message does. The live cutoff is the live symptom's
alert + 30 min, where the symptom is the latest incident created inside the window,
or (if none) the highest-severity alert — computed from tables only, never the
answer key. This is what makes the agent's world honest: nothing after the cutoff
exists in its store.

---

## 5. Layer 3 — How the ontologies are derived

The ontology is built NeOn-style: **competency questions first, a hand-written core,
then data-driven expansion proposed by an LLM and gated by tests.** It lives in
`ontology/` and is treated as code — versioned, reviewed, changed by pull request.

```
  +----------------------------------------------------------------------------+
  |                        ONTOLOGY = CODE  (ontology/)                        |
  |                                                                            |
  |  core.ttl            OWL TBox: classes + properties, hand-written,         |
  |                      aligned to OTel semconv / PROV-O / OWL-Time / SKOS    |
  |  schemes/*.ttl       8 closed SKOS taxonomies = LLM output contracts:      |
  |                      error_category, fault_kind, change_kind, severity,    |
  |                      message_category, sentiment, claim_kind, signal_type  |
  |  shapes.ttl          SHACL: every node typed, every ops:Application in the |
  |                      catalog, provenance required, dependency caps         |
  |  extensions/*.ttl    Approved data-driven extensions (human-approved PRs)  |
  |  proposals/          LLM-proposed extensions, gated, awaiting approval     |
  |  cq/*.rq + expected  15 competency questions as SPARQL tests with          |
  |                      expected results derived from the answer key          |
  +----------------------------------------------------------------------------+
```

### 5.1 Competency questions define "correct"

15 CQs (`ontology/cq/`) are executable tests: each is a SPARQL file whose first
comment is the natural-language question, with a `requires:` line naming the
enrichers that must have run, and an `.expected.json` answer derived from the
scenario's answer key. Examples:

- `01` "Which incidents affected applications owned by Team Bedrock in us-east-1?" —
  the post's own example query.
- `02` "Which changes happened in the downstream dependency closure of the regressed
  app before the regression?"
- `04` "Which engineer resolved an incident similar to this one last quarter, and
  what did they find?"
- `06` "Which hypotheses were retracted in the incident channel?"
- `07` causal chain owners (a constraint check: top chain root, degraded apps, owners).
- `14` "What share of SEV-2 incidents were caused by api-gateway deployments?" — the
  learned prior.

CQs run on Oxigraph over the union of all named graphs plus the ontology, and are
part of the unit test suite — the ontology cannot regress silently.

### 5.2 The hand-written core

`core.ttl` models five families, deliberately:

| Family | Classes |
|---|---|
| **Identity** | `ops:Application` (the only software identity; no generic `Service`), `ops:Team` (a `prov:Agent`), `ops:Person`, `ops:Region`, `ops:Cluster`, `ops:Instance` |
| **Change** (`prov:Activity`) | `ops:Deployment`, `ops:ConfigChange`, `ops:ScalingEvent`, `ops:ExperimentAllocation`, `ops:Commit`, `ops:ClientRelease` |
| **Signal** (`prov:Entity`) | `ops:MetricSeries`, `ops:Anomaly`, `ops:LogPattern`, `ops:LogSpike`, `ops:TraceEdgeFingerprint`, `ops:Alert` |
| **Response** | `ops:Incident`, `ops:Page`, `ops:Message`, `ops:Claim` (+ `ops:Mitigation`), `chat:Thread` |
| **Reasoning** | `ops:Hypothesis`, `ops:CausalChain`, `ops:CausalLink`, `ops:Pattern`, `ops:Finding` |

Key modeling decisions (all mirror the post):

- **No generic `Service` type.** "Service" means four different things across the
  tooling; applications resolve to exactly one type, `ops:Application`, validated
  against the catalog before the entity is created (`ops:inCatalog` is SHACL-required).
- **Raw telemetry stays out.** Signals are fingerprints; they link back via
  `ops:rawSelector` (raw record id or selector JSON) or `prov:wasDerivedFrom`.
- **Closed values are SKOS concepts, never strings** — `ops:severity`,
  `ops:changeKind`, `ops:errorCategory`, `chat:category`, `chat:sentiment`,
  `chat:claimKind`, `ops:signalType` all range over scheme URIs.
- **Changes are identified by their own system's opaque id** (Spinnaker execution
  id, fast-property change id, ASG activity id, allocation id, git sha) — scenario
  change ids never reach the graph.
- **Identity correction history**: an incident's current `ops:affects` is the latest
  affected service; superseded values move to `inc:previouslyAffected`; each
  incident.io record version gets its own `ops:atTime` so "the incident as known at
  t" is queryable.
- **Concepts apart from instances.** TBox + schemes in `ontology/` (human
  timescale); ABox in the store (continuous).

### 5.3 Namespaces and the one URI scheme

Twelve namespaces, one per operational domain (the post's count; the cut is this
repo's own):

```
   ops  app  team  infra  chg  exp  met  log  trc  alrt  inc  chat
   core  |    |     |     |    |    |    |    |     |     |     |
         +-- applications  +-- changes/experiments  +-- alerts +-- chat
```

One URI scheme for everything (`graph/uris.py`):

```
   vocabulary:  https://kg.local/{ns}#            (classes, properties)
   instances:   https://kg.local/{ns}/{kind}/{stable-id}

   app:    https://kg.local/app/application/title-metadata-svc   (catalog id)
   team:   https://kg.local/team/team/team-bedrock               (slug of owner name)
   person: https://kg.local/team/person/priya.nair               (stable handle)
   change: https://kg.local/chg/config_change/hero/<spinnaker-or-fpc-id>
   alerts: https://kg.local/alrt/alert/hero/radar-09000          (<scenario>/<id>)
   skos:   https://kg.local/log/error_category/pool_exhaustion   (scheme concept)
```

Stable ids come from the catalog, source-system ids, or content hashes — **never
from free text**. Derived nodes use content hashes of their inputs. Edges that need
provenance get an `rdf:Statement` reified with the same ids.

### 5.4 Data-driven evolution: mine -> propose -> gate -> approve

`ontology_evolve/propose.py` closes the loop on "ontology as code":

```
   harvest (ABox)                 processed tables
       |                                |
       v                                v
  MINE what the ontology does NOT cover
   * WARN+ log templates filed under the catch-all "other" category
   * metric tag keys / Prometheus labels with no usable property
   * event (kind, type) pairs the changes enricher mapped to no change kind
       |
       v
  LLM proposes Turtle diffs (justification + example values per term)
       |
       v
  GATE (check_proposal), each rejection explained:
    1. parses as Turtle
    2. additive-only guard: every subject is a NEW term (class/property/concept)
       in one of the 12 namespaces, labeled, no blank nodes, no sameAs /
       equivalence / disjointness axioms, concepts only in extensible schemes
       (error_category, change_kind) with notation + label + definition
    3. SHACL + the full CQ suite re-run with the proposal loaded
       |
       v
  HUMAN APPROVAL (manual): move the file into ontology/extensions/
       -> loaded by every subsequent harvest
```

Why the guard when SHACL + CQs already run: those query without reasoning, so an
axiom like `ops:Application owl:equivalentClass ops:Team` changes no CQ row yet
silently redefines the model for reasoning consumers. The additive-only guard is the
only thing that sees it. Four approved examples live in `extensions/`
(`fallback-served`, `retry-in-progress`, `queue-backlog`, `metric-quantile`).

---

## 6. Layer 4 — Ingesting preprocessed data into RDF (the harvest)

### 6.1 The store

`graph/store.py` wraps a **pyoxigraph** store — a Rust quad store with named graphs
and SPARQL 1.1, the closest open analog to the post's QuipuDB-on-redb:

- every enricher owns one named graph, `https://kg.local/graph/<enricher>/<scenario>`;
- `replace_graph` writes each graph **whole** — a rerun replaces, never appends
  (idempotent, convergent);
- the ontology (core + schemes + extensions) sits in its own graph so SPARQL can
  follow `rdfs:subClassOf`;
- `dump_turtle` writes a deterministic, sorted Turtle file per harvest — the post's
  "full Turtle serialization for archival… more useful for debugging than we
  expected."

### 6.2 The enricher framework and the pipeline

The enricher contract is exactly the post's:

```
   fetch(ctx)   -> raw          read noisy inputs (as-of filtered Parquet tables,
                                 or graphs earlier enrichers wrote)
   analyze(raw) -> result       pure computation (may be stochastic)
   emit(result) -> triples      ontology-conformant triples (core.ttl + extensions)
```

`run()` executes the three in order, writes via `replace_graph`, and converts any
exception to `EnrichmentResult(success=False)` — **best effort: one enricher never
blocks the harvest**. A failed enricher's graph is *cleared*, never left stale; an
enricher whose declared `graph:` input is absent fails rather than succeeding
emptily.

Ten enrichers run sequentially (each builds on the last; the ordering buys accuracy
at a wall-clock cost the design accepts, mirroring the post):

```
   #   enricher          reads                        emits (into its named graph)
   --- ----------------   --------------------------   -------------------------------
   1   catalog           catalog.parquet              ops:Application (catalog-gated),
                          (apps.yaml rendered)         ops:Team, ops:Person, regions,
                                                       ownedBy / deployedIn / tier / kind
   2   topology          topology (from catalog +     ops:dependsOn edges (capped 50 per
                          trace edges)                 node per direction, cap recorded)
   3   changes           events parquet               ops:Deployment / ConfigChange /
                                                       ScalingEvent / ExperimentAllocation
                                                       / Commit / ClientRelease, chg:changeId
   4   metrics           fp_metric_anomalies          ops:MetricSeries, ops:Anomaly
   5   logs              fp_log_*                     ops:LogPattern, ops:LogSpike (+ SKOS
                                                       error category per template)
   6   traces            fp_edges                     ops:TraceEdgeFingerprint
                                                       (slow_trace / failed_trace types)
   7   alerts_incidents  events parquet               ops:Alert, ops:Page, ops:Incident
                                                       (+ versions, corrections, openedBy)
   8   chat              chat_messages/claims         chat:Thread, ops:Message, ops:Claim,
                                                       retraction links (chat:retracts)
   9   history           incidents + chat of the      ops:similarTo, ops:resolvedBy, and
                          90-day window                ops:Pattern priors from past incidents
  10   correlation       ALL earlier graphs           ops:CausalChain + ops:CausalLink
                                                       (section 7)
```

### 6.3 Provenance on every fact

```
   node built from raw records  -> one ops:rawSelector per raw record id
                                   "logs|atlas.jsonl|4096|214|9f2b…"
   fingerprint node             -> JSON selector {"source": "logs", "app": …,
                                   "key": …, "t_start": …, "t_end": …}
   node built from other nodes  -> prov:wasDerivedFrom <upstream node>
   derived edge                 -> rdf:Statement carrying the same provenance
```

This is what `raw_evidence` walks in reverse and what the reachability ledger
counts. It is also what keeps the graph auditable: any claim in a report can be
drilled back to the exact raw lines that justify it.

### 6.4 SHACL and the harvest report

After all enrichers run, `pyshacl` validates the union against `shapes.ttl` (with
the ontology mixed in). The three post invariants are enforced as shapes: every node
has `rdf:type`; every `ops:Application` is in the catalog; dependency edges are
capped. Output per scenario: the Oxigraph store at `data/graph/<scenario>/store`, a
`harvest.ttl`, and a `harvest.json` report (quad count, per-enricher results, SHACL
verdict). `harvest --as-of` additionally builds the as-of store at
`data/graph/<scenario>/as_of/` — the store the live RCA reads.

---

## 7. Layer 5 — The correlation engine

`correlate/engine.py` condenses the graph's signals into causal chains. It is
deterministic, beam-search based, and — the addition this repo makes to the post —
**topology-gated**.

### 7.1 Signals and onsets

Thirteen signal types (the post's list). Fingerprint detections fold into **onsets**:
a detection inside a running episode of its series family (a metric name, a log
pattern, a trace edge) is the same condition seen again; episodes of one
`(type, app)` starting in the same minute are one event. Each onset is one `Signal`
with `severity = m / (m + m_fire)` (a signal at its detector threshold is 0.5).
Changes, alerts and incidents are declared events, severity 1.

```
   log_error · metric_anomaly · slow_trace · failed_trace · e2e_bottleneck ·
   alert · incident · deployment · config_change · experiment · autoscaling ·
   commit · error_pattern
```

### 7.2 Admissibility, scoring, gates

An edge `a -> b` ("a may have caused b") exists when:

- **Time:** `a` precedes `b` on the detectors' minute grid, within a 10-minute
  window (`WINDOW_S = 600`); changes land before onsets in their own minute, exact
  pairs must be strictly ordered.
- **Roles:** changes are roots only (nothing causes a deploy — well, except
  autoscaling reacting to config/load); an incident is caused only by the alert its
  opening page names; an incident causes nothing; two onsets of one type on one app
  are one condition, not cause and effect.
- **Topology gate** (the key anti-red-herring mechanism): `b` is on `a`'s app, or
  `b`'s app transitively **calls** `a`'s app — effects travel up the callers. This
  gate is what defeats the android-release and api-gateway traps: a change on
  something the alerting app never calls cannot reach it.
- **Explaining away:** a change may cause `b` only if `b`'s cascade had **not**
  already begun before it — an earlier change's rooted cascade vetoes a later change
  (a red herring is a change that lands while the incident is already unfolding).

```
   pair_score(a, b) = prior(a.type, b.type)
                    * (1 - 0.4 * dt / WINDOW_S)        temporal proximity
                    * (0.5 + 0.5 * severity(b))        severity

   selected priors (the post's pairs + telemetry mesh at 0.75):
     deployment      -> log_error       0.90
     config_change   -> metric_anomaly  0.90
     config_change   -> autoscaling     0.95
     autoscaling     -> metric_anomaly  0.85
     metric_anomaly  -> alert           0.90
     alert           -> incident        0.95
     failed_trace    -> incident        0.85
     telemetry       -> telemetry       0.75
```

A **chain** is a path from a root change (no admissible cause) to the symptom; its
confidence is the **geometric mean** of its pair scores. A backward beam search
(width 20, max length 10) finds the best chain per root; anything under
`MIN_CONFIDENCE = 0.6` is dropped (the post's cutoff). "Late roots" — changes that
precede a cascade origin they don't account for — are excluded. The engine
**proposes with evidence, never concludes**: chains are stored as `ops:CausalChain`
/ `ops:CausalLink` nodes citing every detection behind each link, alongside the
confidence, so consumers can weigh the claim.

---

## 8. Layer 6 — The agentic system (AutoRCA)

### 8.1 Shape: a commander and seven specialists

Built on **DeepAgents** with **GLM-5.3-flash on Fireworks** (via
`langchain-fireworks`). A harness profile removes the shell tool and the
general-purpose subagent — specialists only ever see their own slice of the world.

```
                        +---------------------------------------------------+
                        |              AutoRCA commander                    |
                        |   prompt: prompts/autorca.md (7-step method)      |
                        |   tools: find_symptom, changes_in_window,         |
                        |   causal_chains, candidate_evidence,              |
                        |   read_findings, raw_evidence, sparql_select      |
                        +---------------------------------------------------+
                          | dispatches all 7 in ONE turn (task tool)
          +---------------+---------------+---------------+-----------+
          v               v               v               v           v
   +-----------+   +-----------+   +-----------+   +----------+   +-----------+
   | topology  |   |  change   |   |  metrics  |   |   logs   |   |  traces   |
   | closure,  |   | deploys,  |   | anomalies |   | spikes,  |   | slow/fail |
   | trace edge|   | config,   |   | per app/  |   | raw      |   | edges, raw|
   | degrada-  |   | scaling,  |   | metric    |   | lines    |   | lines     |
   | tion      |   | experi-   |   |           |   |          |   |           |
   |           |   | ments, CRs|   |           |   |          |   |           |
   +-----------+   +-----------+   +-----------+   +----------+   +-----------+
          v               v               v               v           v
          +---------------+------+--------+---------------+-----------+
          v                      v
   +--------------+      +---------------+          each specialist:
   | alerts_      |      |   history     |           * own prompt (prompts/<n>.md)
   | incidents    |      | similar       |           * own tool slice + record_finding
   | alert, page, |      | incidents +   |           * records ops:Finding into its
   | incident     |      | chat claims   |             OWN findings named graph
   +--------------+      +---------------+
                                        |
                                        v
              findings (typed triples about ops:Application)  <-- the interface
              the commander reads with read_findings; specialists never
              talk to each other and need not know the others exist
```

This is the post's "the ontology acts as the interface" made literal: a specialist
records findings **as triples** about an `ops:Application`, validated by SHACL;
another specialist reads those triples; neither needs the other's vocabulary beyond
the graph's.

### 8.2 The commander's method (from `prompts/autorca.md`)

1. **Find the symptom** (`find_symptom`) — the alert, its app, the incident it
   opened (the incident's filed apps are "a person's first guess… treat them as a
   hypothesis to test, never as a fact").
2. **Dispatch all seven specialists** in one turn with the same brief.
3. **Read findings** (`read_findings`), grouped by application.
4. **Propose candidates** — changes from findings, the window, chain roots, filed
   apps, and history's prior kinds. Nothing is dropped for looking unlikely.
5. **Gather facts per candidate** — reach, timing, propagation (call-path status:
   `degraded` / `not_degraded` / `unknown`), what was already happening, what came
   before it, coverage of the whole cascade, and the engine's chains as *one more
   piece of evidence, never the answer*.
6. **Weigh and refute** — the root is the candidate whose facts explain the symptom;
   every other candidate needs a stated refuting fact. Two survivors => lower
   confidence. None => inconclusive.
7. **Return the `RCAReport`** — `root_cause_found | inconclusive`, ranked
   hypotheses each with a causal chain of URIs, confidence, evidence URIs, and
   `refuted_alternatives` ("<uri> refuted: <the fact>").

### 8.3 The tool layer: typed views over the graph

A flash-tier model writes poor SPARQL against an unfamiliar ontology, so tools are
**typed SPARQL wrappers**, not free-form query access:

| Tool | What it gives the model |
|---|---|
| `find_symptom` | alert + incident + symptom URI, as-of |
| `dependency_closure` | transitive callers/dependents with hops, kind, tier |
| `changes_in_window` | changes of the apps in a window (with `what` digest) |
| `anomalies_in_window` | metric anomalies folded per app/metric/direction |
| `log_spikes` | spiked log patterns with counts, sigmas, categories, templates |
| `trace_edges` | slow/failed caller→callee edges with p50/p99/error rate |
| `alerts_incidents` | alerts + incidents in a window, time-ordered |
| `similar_incidents` | past incidents by cause app/kind + learned patterns |
| `chat_claims` | typed claims of a channel (kinds filtered, quotes kept) |
| `causal_chains` | engine's ranked chains for a symptom (top 5) |
| `candidate_evidence` | **facts only** about changes the model names: minutes before onset/alert, call path status, rooted/unrooted prior telemetry, same-app changes, engine chain |
| `raw_evidence` | the original raw lines behind a node / selector / raw id (DuckDB over Parquet + verified raw bytes) |
| `sparql_select` | read-only SELECT escape hatch, LIMIT 200 enforced |
| `record_finding` / `read_findings` | write/read `ops:Finding` (SHACL-checked before store) |

Tool-discipline details that keep a stochastic agent inside a deterministic contract:

- **As-of only.** The agent opens `data/graph/<scenario>/as_of/store` read-only and
  works on a private in-memory copy; its findings never reach disk and no earlier
  run's findings leak in. `raw_evidence` re-enforces the cutoff itself (only records
  whose every parsed row precedes the cutoff are returned).
- **Budgeted output.** Every result is compact JSON capped (~4000 chars; LIMIT 200);
  truncation is reported on the last row (`{"truncated": n}`, `{"capped": 200}`).
- **Self-correctable errors.** Bad calls return `"error: <what to change>"` instead
  of raising, so the model can fix itself.
- **Nickname-aware.** App arguments go through the L3 `Resolver`, so "TMS" works.
- **`/evidence/`** maps to a fresh temporary directory (a DeepAgents
  `FilesystemBackend` route) where the agent can stash scratch findings per run.
- **Guardrails.** Recursion limit 200; a run that ends without a report becomes a
  scored `FailedRCA` (`verdict="inconclusive"`), never an exception.
- `candidate_evidence` **proposes nothing** — the model decides; no tool is an
  authority to defer to. The `graph_no_evidence_tool` arm runs the same agent
  without this tool as an ablation.

### 8.4 Structured output

The final answer is bound by a pydantic schema via `ToolStrategy(RCAReport)`
(deliberately not the provider's JSON mode — with a bound schema the flash model
wrote the report in one shot with no tool calls). Every hypothesis must cite graph
URIs; the scorer checks that cited evidence reaches answer-key evidence raw ids.

### 8.5 The fast path (`agent/fastpath.py`, the Deep Path's happy-path shortcut)

Same tools, same store, same report schema — but by the time a specialist's tool
loop ends, everything it learned was computable deterministically in advance. The
LLM's irreplaceable job is judgment, not fetching (`fastpath.specialist_slice`
runs each specialist's own tool slice and `candidate_evidence` in chunks, with
the real tools, before any model is consulted):

```
  t=0.0s  PREFETCH (deterministic tool calls, ~1s)      no LLM
  t=1.0s  7 SPECIALISTS IN PARALLEL (one-shot each)     wall = slowest ≈ 4s
          slice in the prompt -> FindingIns tool call;
          findings recorded on a private store copy,
          validated by the same SHACL record_finding
  t=5.0s  SYNTHESIS (1 call, prompts/autorca_fast.md)   ≈ 12s
          chains + changes + candidate facts + findings
          -> RCAReport; weak (conf < 0.6, top-two tie
          <= 0.05, inconclusive) -> ESCALATE to run_rca
```

Measured on the hero alert: **43 -> 8 model calls, 410K -> 33K input tokens,
~32.5 s -> ~17-20 s wall**, correct root, no escalation. The remaining wall is
two GLM decode phases (~60 tok/s), not structure; the contingencies in
PROPOSAL-fast-rca-and-evidence-graph.md §A.3 (skip specialists on corroborated
engine chains, a smaller specialist model, token streaming) are the path below
10 s. Escalation is proven, not assumed: a weak synthesis hands the alert to the
Deep Path over a fresh store copy, so no fast-path finding reaches it.

Evals: the `graph_fast` arm is **opt-in** (`e2ekg eval --arms graph_fast`) —
`all` keeps the Deep Path arms the evals have always measured.

---

## 9. Layer 7 — The knowledge flywheel

The post's four phases — Observe, Enrich, Infer, Adapt — map onto the pipeline:
Observe = generate/collect, Enrich = the enricher ladder, Infer = correlation,
**Adapt = `flywheel/adapt.py`**:

```
        +-------+        +--------+        +-------+        +--------+
        |Observe|------->| Enrich |------->| Infer |------->| Adapt  |
        | MELT  |        | typed  |        | causal|        | approved
        | signals        | triples|        | chains|        | roots become
        +-------+        +--------+        +-------+   +----| Patterns
             ^                                             |
             |                                             v
             +------------------------------------- priors for the next
                                                    harvest (history enricher
                                                    + similar_incidents)
```

- A chain is **confirmed** when it is top-1 correct **and** a human approved it
  (`data/evals/approvals.jsonl`: scenario, root URI, approver, time). Correct is not
  enough — the approval stands in for the ground truth a real incident wouldn't have.
- `e2ekg adapt` writes one `ops:Pattern` per confirmed `(change kind, app,
  severity)` into the **history** store's flywheel graph, with `support` and
  `frequency` (share of that severity's resolved incidents) — the same shape the
  history enricher's priors use ("api-gateway deploys cause 40% of SEV-2s").
- Patterns change the priors the next investigation reads; withdrawing an approval
  withdraws the pattern (graphs are replaced whole, so inputs fully determine output).
- The flywheel **refuses to write to as-of stores** — the stores live evals read
  must stay what was observable at their cutoff.

---

## 10. Layer 8 — Evaluation harness

`e2ekg eval` scores six arms across all scenarios (resumable, atomic run files):

```
   LLM arms (per-run, GLM)                    deterministic engine arms (one run)
   --------------------------                 ------------------------------------
   graph                    the full agent    engine_top                chains in order
   graph_no_evidence_tool   agent w/o         engine_no_topology        topology gate OFF
                            candidate_        engine_no_explain_away    explaining away OFF
                            evidence
   raw_only                 one agent over the as-of RAW files (no graph at all);
                            compare against graph_no_evidence_tool, not graph
```

Scoring against the (held-out) answer key:

| Metric | Meaning |
|---|---|
| `top1` / `top3` | true root is the first / among first three hypotheses (control scenario: a correct abstention) |
| `evidence_precision` | share of the top hypothesis's cited evidence that reaches answer-key evidence |
| `trap` | picked a planted red herring as #1 |
| `control_false_positive` | claimed a root in the no-root scenario |

Evidence checking is transitive: a cited graph URI counts when it — or anything it
`cites` or `prov:wasDerivedFrom` (up through fingerprint selectors) — reaches a
key evidence raw id; `path:line` citations count by byte-range overlap with the
segment map of the as-of raw view.

**Known result** (run `recovery-1run`, one run per arm): raw_only also reached 7/7
top-1, so on this data the *engine* ablations — 2/7 top-1 without the topology gate
— are what demonstrate the graph's value; the agent-arm ranking needs repeated runs
(`--runs 3`) before conclusions are drawn. This honest note is part of the eval
design: one run per stochastic arm proves nothing.

---

## 11. Layer 9 — Serving: API and UI

```
   +---------------------------+          +------------------------------+
   | FastAPI  (e2ekg.api.app)  |          | Next.js console (ui/)        |
   |---------------------------|  HTTP    |------------------------------|
   | /api/scenarios            |<---------| /              scenario list |
   |   /{s}  symptom, chains,  |          | /scenarios/[name]  detail    |
   |         changes near it   |          |   /case          RCA case    |
   |   /topology  /alerts      |          | /ontology  TBox + schemes    |
   |   /case /after /lineage   |          | /sparql    query console     |
   |   /scorecard              |          | /evals     run summaries     |
   | POST /{s}/sparql          |          +------------------------------+
   | POST /{s}/rca  (background job: AutoRCA via Fireworks)   |
   | GET  /api/jobs/{id}       |
   | GET  /api/ontology, /api/evals[/run]                     |
   +---------------------------+
```

Two properties worth noting:

- **The API serves through the agent's own tools** (`make_tools`), so the UI shows
  exactly what the agent can see — one read path, one set of caps and cutoffs.
- The **case graph** endpoint reconstructs an RCA run's reasoning (findings, chains,
  evidence) from the trail and findings graph; **lineage** walks a node back through
  `prov:wasDerivedFrom` / `ops:rawSelector` toward raw bytes.

---

## 12. Cross-cutting invariants

The rules that make the whole stack trustworthy (each enforced somewhere concrete):

1. **Answer-key isolation.** Pipeline code never reads `data/raw/_answers/`; only
   the scorer does. Symptom and cutoff are computed from tables alone.
2. **Identity before everything.** One authoritative catalog per entity kind; an
   unresolved surface form stays a literal, never becomes an application.
3. **Nothing lost.** Immutable raw + lossless parse + quarantine-with-reason +
   byte-coverage and reachability proofs, gated on every ingest.
4. **Provenance everywhere.** Every derived fact carries `ops:rawSelector` or
   `prov:wasDerivedFrom`; edges carry theirs on reified statements.
5. **Raw volume never enters the graph.** Fingerprints + selectors only.
6. **Closed contracts for stochastic tools.** LLMs emit SKOS concepts, pydantic
   schemas and SHACL-conformant triples — or land in quarantine. The five-layer
   entropy ladder turns stochastic steps into a deterministic, queryable graph.
7. **Sequential, idempotent, best-effort enrichment.** Each enricher replaces its
   own named graph; failures clear it and never block the harvest.
8. **As-of honesty.** The agent's store holds only what was observable at the
   cutoff; log templates re-mined as-of; raw reads re-enforce the cutoff.
9. **Ontology as code.** Additive-only, gated by SHACL + competency questions,
   human-approved; concepts and instances evolve on different clocks.
10. **Humans approve; agents propose.** The flywheel only learns from human-approved
    root causes; hypotheses always carry the evidence that produced them.

---

## 13. Operating the system

```
   # full pipeline (all scenarios, scripted LLM, no network)
   scripts/ingest.sh

   # with live LLM extraction (Fireworks GLM)
   FIREWORKS_API_KEY=... scripts/ingest.sh --llm fireworks

   # individual steps
   uv run e2ekg generate   --scenario hero --seed 7
   uv run e2ekg preprocess --scenario all --llm scripted
   uv run e2ekg ledger     --scenario all                 # the conservation gate
   uv run e2ekg harvest    --scenario all                 # full history stores
   uv run e2ekg harvest    --scenario all --as-of         # as-of stores for AutoRCA
   uv run e2ekg ontology   propose --llm fireworks        # mine -> propose -> gate
   uv run e2ekg rca        --scenario hero --alert radar-09000
   uv run e2ekg eval       --scenarios all --runs 3

   # serving
   scripts/start-backend.sh && scripts/start-ui.sh
```

Test suites mirror the layers (`tests/scenario`, `tests/generate`, `tests/preprocess`,
`tests/graph`, `tests/ontology`, `tests/correlate`, `tests/explain`, `tests/agent`,
`tests/evals`, `tests/flywheel`, `tests/api`) — including the ledger proofs, the
recovery-splice byte-identity test, lossless-parser tests, CQ regression tests, and
agent leak tests (the agent must never see post-cutoff or answer-key facts).

---

## 14. Sources

- Netflix TechBlog, *Ontology-Driven Observability: Building an End-to-End Knowledge
  Graph at Netflix Scale* — full text: `docs/netflix_post.md`;
  https://medium.com/netflix-techblog/ontology-driven-observability-building-an-end-to-end-knowledge-graph-at-netflix-scale-421392cf5adf
- Compact design brief: `docs/DESIGN.md`
- RCAEval benchmark: https://arxiv.org/abs/2412.17015
- DeepAgents docs: https://docs.langchain.com/oss/python/deepagents/overview
- OpenTelemetry semantic conventions, PROV-O, OWL-Time, SKOS, SHACL, SPARQL 1.1
