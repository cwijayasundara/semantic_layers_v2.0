# E2E Knowledge Graph Prototype: Design

Goal. Rebuild a realistic version of Netflix's ontology-driven observability system. Generate messy MELT data that hides one true root cause among red herrings. Preprocess it with nothing lost. Derive a taxonomy and ontology from it. Build an RDF graph. Then let a DeepAgents agent on `glm-5.3-flash` (Fireworks) find the right answer, scored against a held-out answer key.

## 1. What Netflix actually built (sources at the end)

- Problem. A home screen (LOLOMO) time-to-render regression of about 1.5 s took about 4 hours, 9 paged teams and 30+ engineers to diagnose. At the same moment 12 other metrics moved across 5 services. 47 events happened just before. A connection-pool-exhaustion error spiked in 1 of 7 services, with timeouts elsewhere. The root cause was a service that reduced its capacity, and the errors propagated backward to the client.
- Stack. RDF + OWL + SHACL + SPARQL, Turtle dumps per harvest, quad store (QuipuDB on redb). Twelve namespaces (not named publicly). One URI scheme.
- Identity first. Every `ops:Application` must match the software catalog (about 49k entries). No generic "Service" type. Unvalidated strings never become applications.
- Cardinality control. Raw high-volume telemetry stays out of the graph. The graph stores fingerprints of patterns and links to the raw store.
- Enrichers. Dozens of them, ordered and sequential, best effort. Each reads noisy input and emits typed triples. Later enrichers read earlier output.
- Constrained LLMs. LLM output must land in closed taxonomies (for example a thread category is one of incident, question, discussion, decision). "Stochastic tools to produce a deterministic, SPARQL-queryable graph."
- Correlation engine. 13 signal types, prior-weighted causal pairs (deploy then log error, config change then metric anomaly, failed trace then incident), 10 minute window, chains under 0.6 confidence dropped.
- Flywheel. Observe, Enrich, Infer, Adapt. Learned patterns ("api-gateway deploys cause 40% of SEV-2s", "30% of deployment issues in api-gateway come from Connection Timeouts") become priors. Hypotheses can be wrong, so every one carries its evidence.
- Source systems, each "internally coherent and externally isolated". Atlas (time series), Radar (alert thresholds and firing history), incident.io (severities, responders), PagerDuty (rotations), Spinnaker (what shipped when), Slack (who solved something similar last quarter).
- Why no Service type. "Service" means four different things across their tooling. Names differ "by a suffix, a casing convention, or a rename only a couple of people remember".
- Entropy ladder. Five layers drive entropy (share of information still unstructured) down. Each step is stochastic, the chain is deterministic. The five layers are only in an image, so section 4 maps our steps onto my own reconstruction of them.
- Enricher contract. `fetch` noisy input, `analyze` (may be stochastic), emit typed triples, return `EnrichmentResult(success, triples_added)`. Load graph, add, save, sequentially.
- Closed outputs shown verbatim. `category IN [incident, question, discussion, decision]`, `sentiment IN [positive, neutral, negative, urgent]`, plus `slack:isIncidentRelated` as `xsd:boolean`.
- AutoRCA. Seven specialists ("the other six"). The ontology is the interface between them. Each writes findings as triples about an `ops:Application`, and none needs to know the others exist.
- The alert fires at 3:30pm. Humans still weigh evidence and write the fix. The graph absorbs the hour or two of context assembly.

## 2. The data shape comes first

Everything hangs off one typed **Scenario** model. The generator renders it into raw signals. The answer key is the scenario itself, held out from the pipeline.

```
Scenario
  catalog:      Application[] (id, aliases[], owner team, tier, region[])
  topology:     DependencyEdge[] (caller -> callee, protocol, pool size)
  timeline:     Window (start, end, resolution)
  root_cause:   Fault (kind, target app, start time, mechanism)
  propagation:  derived from topology + fault (who degrades, when, how)
  red_herrings: Change[] (client release, A/B rollout, api-gateway deploy, chronic error)
  messiness:    MessInjection[] (injector id, target, params)
  answer_key:   { root_cause_uri, causal_chain[], must_cite_evidence[] }
```

Fault propagation is computed from the topology, so metrics, logs, traces and events stay mutually consistent. Real incidents look like this, and it is what makes the needle findable.

### Hero scenario (mirrors the post)

- 6 hops. Client (tv, android, ios, web) to edge-gateway to homepage-graphql (federated) to lolomo-svc to row-ranker to title-metadata-svc.
- Root cause. A config change lowers `title-metadata-svc` autoscaler min instances from 48 to 12 at T0. Evening traffic rises. Upstream connection pools in `row-ranker` exhaust. Timeouts climb back up the chain. homepage-graphql returns partial rows. LOLOMO TTR p90 goes up about 1.5 s.
- Red herrings, each designed to fool a naive method:
  - An android client release 8 minutes before (temporal proximity trap).
  - An A/B allocation change on a ranking experiment (plausible-mechanism trap).
  - An api-gateway deploy (the learned "40% of SEV-2s" prior trap).
  - A chronic `NullPointerException` in artwork-svc that is always there (volume trap).
  - Edge load shedding (a symptom that looks like a cause).
- Alert fires at 3:30pm local (15:30 PT, logged by different systems in UTC, PT and epoch ms).
- Slack channel. About 200 messages from 50 people touching 15 services. Nicknames ("TMS", "the ranker"), typos, wrong guesses that get retracted, a mitigation ("bump min to 40") posted before anyone names the cause.
- Incident history. 90 days of past incidents (about 30), each with its own alert, incident record and Slack channel. It does two jobs. It manufactures the misleading prior (api-gateway deploys really do cause about 40% of past SEV-2s). It also hides the shortcut, one similar title-metadata capacity incident last quarter, solved by one engineer whose thread names the fix.

Variant scenarios (5 to 10, seeded) change the fault kind (bad deploy, cache eviction storm, DB hot partition, cert expiry, retry storm) so the system isn't tuned to one story. RCAEval's fault taxonomy (CPU, MEM, DISK, DELAY, LOSS, SOCKET, code-level) is a good menu.

## 3. Messiness: a registry, not ad hoc noise

Each kind of mess is a registered injector with an id. The answer key records every injection. That makes "did preprocessing undo it without losing data" a testable question.

| Signal | Injectors |
| --- | --- |
| Identity | Same app as `title-metadata-svc`, `TitleMetadataSvc`, `titlemetadata`, `TMS`, `tms-prod-v042`, ASG name, k8s pod name. Application in Atlas, service in Radar, cluster in Spinnaker. Stale catalog alias. A rename only Slack remembers. Team renamed mid-window. |
| Metrics (Atlas-like + Prometheus text) | Tag key drift (`nf.app`, `app`, `service`). ms vs s units. Counter resets on restart. Gaps. Late data. Duplicate series. High-cardinality tags (per-instance). |
| Logs | Per-service formats (JSON, logfmt, log4j with multi-line stack traces, nginx access, syslog). Mixed timezones and epoch ms. Clock skew per host. Truncated lines. At-least-once duplicates. Interleaved lines. PII in fields. |
| Traces | OTLP JSON plus legacy Zipkin v2. Head sampling 1% plus tail sampling for errors. Orphan spans (missing parents). Child before parent from skew. `service.name` aliases. |
| Events | Spinnaker-like deploy JSON, property change log, ASG scaling events, A/B allocation log, alerts, PagerDuty pages, incident record, git commits. Missing fields, free-text descriptions, duplicate webhooks. |
| Chat | Nicknames, typos, emoji, threads, edits, retractions, links to dashboards and alert IDs. |

Calibration for realism. Log templates and their frequency distribution from LogHub (HDFS, OpenStack, Spark sets). Metric shapes (diurnal cycle, noise, step changes) and fault signatures from RCAEval RE2. OpenTelemetry semantic conventions for span and resource attributes. Default size is laptop scale, about 2 to 5M log lines, a few thousand series, about 200k spans per scenario.

## 4. Preprocessing without losing data

"Don't lose data" has a precise meaning here. Putting every log line into RDF is the wrong reading. Netflix explicitly avoids it, and it would bury the needle again. The contract is:

1. **Immutable raw layer.** Raw files are never modified. Every raw record gets a stable id `(source, file, byte offset, sha256)`.
2. **Lossless parse.** Every raw record becomes exactly one parsed row in Parquet, or one quarantine row with a reason. Fields the parser doesn't understand go into a `_residual` column. Nothing is dropped silently.
3. **Keep both.** Normalized values sit next to originals. Normalized timestamp plus original string plus estimated host skew. Resolved app URI plus surface form plus resolution method plus confidence.
4. **Provenance on every fact.** Every triple derived from data lives in a named graph per enricher run and points to its raw record ids (PROV-O `prov:wasDerivedFrom`).
5. **Conservation ledger.** A check script asserts per source that raw count = parsed + quarantined, and that every parsed row is reachable from the graph, either through a fingerprint or directly. This is the gate for every later step.

Entropy ladder (my reconstruction of Netflix's five layers, since theirs is only in an image):

| Layer | What it turns into structure | Determinism comes from |
| --- | --- | --- |
| L1 Parse | Raw bytes into records with ids | Parser registry, quarantine for the rest |
| L2 Normalize | Times, units, field names | Rules, originals kept |
| L3 Resolve identity | Surface names into catalog URIs | Catalog gate, closed choice for the LLM |
| L4 Fingerprint | Volume into patterns (templates, anomalies, edge stats) | Deterministic algorithms with fixed seeds |
| L5 Interpret | Chat and free text into typed claims | Closed SKOS schemes plus SHACL |

Steps, in order:

| Step | Method | Output |
| --- | --- | --- |
| Format detection | Per-file sniffing plus a parser registry | parsed rows, quarantine |
| Time normalization | Parse all formats to UTC. Estimate per-host skew from trace parent/child pairs and NTP-like log markers. Correct, keep original. | `ts_utc`, `ts_raw`, `skew_ms` |
| Identity resolution | Deterministic first (regex for ASG, pod and version suffixes, exact alias table), then catalog fuzzy match, then LLM only for leftovers with a closed choice list. Unresolved stays a literal, never an app. | `app_uri`, `surface`, `method`, `confidence` |
| Log templating | Drain3 mines templates. Each line becomes template id plus parameters. Raw line kept. | templates, per-window counts |
| Metric cleanup | Unit normalization, counter reset handling, gap marking (no interpolation), dedupe. Anomaly detection (robust z-score or BARO-style change point) per series. | clean series, anomaly fingerprints |
| Trace repair | Rebuild trees, mark orphans, skew correct, aggregate into per-edge latency and error fingerprints. | span trees, edge fingerprints |
| Chat | LLM extracts claims, hypotheses, mitigations and mentioned entities into closed categories with structured output. Quote spans kept. | typed message facts |

## 5. Deriving the taxonomy and ontology

Recommended method. Competency questions first, a hand-written core, then data-driven expansion proposed by the LLM and gated by review. This is the NeOn / competency-question approach, and it matches what Netflix describes (open world plus human review, smaller ontologies stabilize faster).

1. **Competency questions (CQs).** Write 15 to 25 questions the graph must answer, each as a SPARQL test with an expected result from the answer key. CQ 1 is the post's own. "Which incidents affected applications owned by Team Bedrock in us-east-1?" Others: "Which engineer resolved an incident similar to this one last quarter, and what did they find?" "What changed in the downstream dependency closure of homepage-graphql in the 30 minutes before the TTR regression?" "Which apps own the log patterns that spiked inside the window?" "Who first proposed the mitigation that worked?"
2. **Hand-written core (OWL, Turtle in git).** Identity classes first. `Application`, `Team`, `Person`, `Region`, `Cluster`, `Instance`. Then changes (`Deployment`, `ConfigChange`, `ScalingEvent`, `ExperimentAllocation`, `Commit`, `ClientRelease`), signals (`MetricSeries`, `Anomaly`, `LogPattern`, `LogSpike`, `TraceEdgeFingerprint`, `Alert`), response (`Incident`, `Page`, `Message`, `Claim`, `Mitigation`), and reasoning (`Hypothesis`, `CausalChain`, `Pattern`). Align with OpenTelemetry resource attributes, PROV-O, OWL-Time and SKOS rather than inventing.
3. **Taxonomies as SKOS concept schemes, closed.** Error category, fault kind, change kind, severity, message category (incident, question, discussion, decision) and sentiment (positive, neutral, negative, urgent), the last two verbatim from the post. Every LLM classification must pick a concept URI from a scheme.
4. **Data-driven expansion.** Mine candidates from the preprocessed data. Drain templates clustered into error concepts. Metric tag keys into property candidates. Event types into change subclasses. The LLM proposes additions as a Turtle diff with justification and examples. SHACL shapes plus the CQ test suite run on the diff. A human (you) approves. This is "ontology as code".
5. **Concepts apart from instances.** TBox and SKOS schemes in `ontology/`. ABox in the store. They change at different speeds.

Proposed 12 namespaces (Netflix's list isn't public, so this is my guess at a sensible cut): `ops` (core), `app`, `team`, `infra`, `chg` (changes), `exp`, `met`, `log`, `trc`, `alrt`, `inc`, `chat`.

URI scheme. `https://kg.local/{ns}/{kind}/{stable-id}`. Ids come from the catalog or from content hashes, never from free text.

## 6. Building the RDF

- Store. Oxigraph via `pyoxigraph`. It is a Rust quad store with named graphs and SPARQL 1.1, the closest open analog to QuipuDB on redb. `rdflib` for tests and small manipulation. `pyshacl` for validation.
- Enrichers. Same contract as the post (`fetch`, `analyze`, emit typed triples, return `EnrichmentResult(success, triples_added)`). An ordered registry. Each enricher declares reads and writes, runs sequentially, and replaces its own named graph per run, so reruns converge (idempotent). External calls (LLM) are best effort with a cache keyed on input hash.
- Invariants checked by SHACL after every harvest. Every node has `rdf:type`. Every `ops:Application` is in the catalog. Every derived fact has `prov:wasDerivedFrom`. Dependency edges capped per node (popular services otherwise dominate).
- Turtle dump per harvest for diffing.
- Correlation engine. Deterministic SPARQL plus Python over the 13 signal types. Causal pair priors, 10 minute window, temporal proximity times severity, 0.6 cutoff, as in the post. Add one thing the post implies but doesn't spell out, topology gating. A candidate cause must sit in the dependency closure of the symptom. That gate is what beats the android-release and api-gateway traps.

## 7. Agentic layer (DeepAgents + GLM-5.3-flash on Fireworks)

```python
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend, FilesystemBackend

agent = create_deep_agent(
    model="fireworks:accounts/fireworks/models/glm-5p3-flash",  # verify id in Fireworks console
    tools=[find_symptom, read_findings, raw_evidence, sparql_select],
    system_prompt=AUTORCA_PROMPT,
    subagents=[topology_agent, change_agent, metrics_agent, logs_agent,
               traces_agent, alerts_incidents_agent, history_agent],
    backend=CompositeBackend(default=StateBackend(),
                             routes={"/evidence/": FilesystemBackend(root_dir=EVIDENCE_DIR, virtual_mode=True)}),
)
```

- Tools are typed SPARQL wrappers, not free-form SPARQL. A flash model writes poor SPARQL against an unfamiliar ontology. `sparql_select` stays as a read-only escape hatch with a forced LIMIT. `raw_evidence(prov_id)` drills from a graph fact back to raw lines through DuckDB, which is where "nothing lost" pays off.
- Seven specialists, matching AutoRCA. Topology (blast radius, dependency closure), change (deploys, config, scaling, experiments, commits, client releases), metrics, logs, traces, alerts and incidents, history (past incidents, Slack reasoning, learned patterns). Each gets only its own typed tools.
- The graph is the interface. Specialists don't return prose to each other. Each calls `record_finding`, which writes an `ops:Finding` into its own named graph, typed against the ontology and validated by SHACL, about an `ops:Application` with evidence prov ids. The orchestrator reads findings with `read_findings`, assembles chains, and runs the refutation pass itself (topology plus timing checks). This is the post's "each specialist learns one vocabulary" design, and it keeps a flash model's output inside a closed contract.
- Output. A structured `response_format` with ranked hypotheses, each with a causal chain of URIs and evidence prov ids.
- Evals. Run all seeded scenarios. Score top-1 and top-3 root cause accuracy, evidence precision (cited ids that are in the answer key), and trap rate (picked a red herring). Two ablations prove the graph earns its keep. Agent over raw files only, and correlation engine without topology gating.

## 8. Build order (each unit ends in a check)

| Unit | Deliverable | Check |
| --- | --- | --- |
| 1 | Scenario model, topology, propagation, 90 day incident history, answer key | Propagation is consistent (every symptom traces to the fault through edges) |
| 2 | Signal renderers plus messiness registry | Each injector visible in output. Sizes match targets. |
| 3 | Preprocessing plus conservation ledger | 100% of raw records accounted for, per source |
| 4 | Core ontology, SKOS schemes, SHACL, CQ tests | CQ tests run (expected to fail until unit 5) |
| 5 | Enrichers, Oxigraph build, correlation engine | SHACL clean. CQs pass. True chain ranks first with topology gating. |
| 6 | DeepAgents AutoSRE plus evals | Accuracy and trap rate across all scenarios, with ablations |
| 7 | Flywheel (Adapt) | Learned patterns change priors without breaking the variant scenarios |

Stack. Python 3.12, uv, polars, DuckDB, Parquet, drain3, pyoxigraph, rdflib, pyshacl, deepagents, langchain-fireworks, pytest.

## Sources

- Netflix TechBlog post, full text pasted by the user (images not included). Syndicated copy: https://noise.getoto.net/2026/10/16/ontology-driven-observability-building-an-end-to-end-knowledge-graph-at-netflix-scale/
- QCon London 2026 talk on InfoQ: https://www.infoq.com/presentations/netflix-observability-aiops-ontology-scale/
- RCAEval benchmark: https://arxiv.org/abs/2412.17015
- DeepAgents docs: https://docs.langchain.com/oss/python/deepagents/overview , subagents and backends pages
- Fireworks GLM 5.3 Flash id (third-party listing): https://www.typingmind.com/guide/fireworks-ai/accounts-fireworks-models-glm-5p3-flash
