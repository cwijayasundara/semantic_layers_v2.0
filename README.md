# e2ekg: ontology-driven observability knowledge graph + AutoRCA

e2ekg generates realistic, messy observability telemetry (metrics, events, logs, traces, Slack,
incidents) that hides one true root cause among red herrings. It preprocesses that data without
losing any of it, then builds an RDF knowledge graph from it: an OWL ontology, SKOS taxonomies
and the harvested data, all validated with SHACL. An AutoRCA agent (DeepAgents on GLM via
Fireworks) investigates alerts over that graph. A FastAPI backend and a Next.js UI let you browse
scenarios, causal chains, the ontology, SPARQL and eval results.

See [docs/DESIGN.md](docs/DESIGN.md) for the full design.

```
data/raw  ──preprocess──▶  data/processed  ──harvest──▶  data/graph/<scenario>/store        (full history)
(generated MELT)           (parquet, lossless)            data/graph/<scenario>/as_of/store  (what the agent sees)
                                                                     │
                                         FastAPI  (:8000)  ◀─────────┘
                                             ▲
                                         Next.js UI (:3000, proxies /api/*)
```

You don't need Docker or a database server. The RDF store is
[Oxigraph](https://github.com/oxigraph/oxigraph), embedded through `pyoxigraph`, and it lives on
disk under `data/graph/`. Setting up the infrastructure means creating the Python environment and
building those stores.

---

## Quick start (three scripts)

```bash
# 1. Once: set up the env and ingest everything into the RDF store
scripts/ingest.sh                 # or: scripts/ingest.sh --scenario hero  (much faster)

# 2. Terminal 1: start the API on :8000 (reads the stores, never re-ingests)
scripts/start-backend.sh

# 3. Terminal 2: start the UI on :3000
scripts/start-ui.sh
```

Then open **http://localhost:3000**. The step-by-step guide below explains each stage.

---

## Step-by-step guide

### Step 1: Install the prerequisites

| Tool | Version | Used for | Install (macOS) |
|------|---------|----------|-----------------|
| [uv](https://docs.astral.sh/uv/) | 0.13+ | Python env and dependencies | `brew install uv` |
| Python | 3.12 (pinned in `.python-version`) | backend, pipeline | `uv` installs it for you |
| Node.js | 20+ | UI | `brew install node` |
| Disk space | about 8 GB for all 7 scenarios at full scale | `data/` | |

Check them:

```bash
uv --version && node -v
```

### Step 2: Clone the repo and open it

```bash
git clone <repo-url> semantic_layers_v2.0
cd semantic_layers_v2.0
chmod +x scripts/*.sh
```

### Step 3 (optional): Add a Fireworks API key

The key is only needed for the LLM features: **AutoRCA runs** from the UI or CLI, and
`--llm fireworks` during ingestion. Everything else works without it, including the graph,
causal chains, SPARQL, ontology and eval views.

```bash
echo 'FIREWORKS_API_KEY=fw_...' > .env
```

`.env` is git-ignored. All the scripts load it automatically.

### Step 4: Set up the infrastructure and ingest the data into the RDF store

```bash
scripts/ingest.sh
```

You only need to do this once (or when you want to rebuild the data). The script runs these stages in order. Each one prints a `==>` header.

1. **Python environment**: `uv sync` creates `.venv` from `uv.lock`.
2. **Generate raw MELT data** into `data/raw/<scenario>/`. That covers the `hero` scenario plus
   the variants `v-bad-deploy`, `v-cache-storm`, `v-hot-partition`, `v-cert-expiry`,
   `v-retry-storm` and `v-control`. Scenarios that are already on disk are skipped.
3. **Preprocess**: lossless parsing, time normalization, identity resolution and chat labels,
   written to `data/processed/<scenario>/` as Parquet.
4. **Ledger**: proves byte coverage of the raw files and fingerprint reachability. It fails the
   run if any data was lost.
5. **Harvest the RDF knowledge graph**: loads the ontology (`ontology/core.ttl` and
   `ontology/extensions/`), the SKOS taxonomy schemes (`ontology/schemes/*.ttl`) and the
   enriched data into Oxigraph at `data/graph/<scenario>/store`, then validates it against
   `ontology/shapes.ttl` (SHACL).
6. **Harvest the as-of store**: `data/graph/<scenario>/as_of/store` holds only what was
   observable at alert time + 30 min. It is the store the RCA agent reads.
7. **Ontology checks**: SHACL shapes and the competency questions in `ontology/cq/`
   (`pytest tests/ontology`).

A full run over all 7 scenarios takes a while and writes about 8 GB. For a quicker first run, use
one scenario:

```bash
scripts/ingest.sh --scenario hero
```

**Options:**

| Option | Default | Meaning |
|--------|---------|---------|
| `--scenario NAME` | `all` | `hero`, one `v-...` variant, or `all` |
| `--llm scripted\|fireworks\|none` | `scripted` | Chat labelling during preprocess. `scripted` is deterministic and needs no network. `fireworks` needs the key. `none` skips chat labels. |
| `--seed N` | `7` | Generator seed |
| `--scale X` | `1.0` | Background-traffic multiplier. Use `0.2` for a small, fast dataset. With any scale other than `1.0`, also pass `--skip-tests`, because the competency-question answers are derived at full scale. |
| `--clean` | off | Delete `data/raw`, `data/processed` and `data/graph` and rebuild (keeps `data/evals`) |
| `--skip-tests` | off | Skip the ontology checks |

### Step 5: Start the backend API

```bash
scripts/start-backend.sh
```

This only starts the FastAPI server on http://127.0.0.1:8000 (docs at
http://127.0.0.1:8000/docs) over the stores in `data/graph/`. It never re-ingests, so restarts
are fast.

**Environment variables for the API:**

| Variable | Default | Meaning |
|----------|---------|---------|
| `API_HOST` | `127.0.0.1` | Bind address |
| `API_PORT` | `8000` | Port |
| `RELOAD` | `0` | `1` restarts the server automatically when code changes |
| `E2EKG_DATA` | `./data` | Data root the API reads |
| `E2EKG_RCA_WORKERS` | `1` | How many AutoRCA jobs can run at once |

Check that the backend is up:

```bash
curl http://127.0.0.1:8000/api/status
# {"data_root": ".../data", "scenarios": ["hero", ...], "fireworks_key": true, "jobs_running": 0}
```

### Step 6: Start the UI

In a **second terminal**:

```bash
scripts/start-ui.sh
```

The first run installs the npm dependencies. The script then starts the Next.js dev server on
**http://localhost:3000**. The browser only talks to Next, which proxies `/api/*` to the backend.

| Variable | Default | Meaning |
|----------|---------|---------|
| `PORT` | `3000` | UI port |
| `API_URL` | `http://127.0.0.1:8000` | Where the backend runs. Change it if you moved `API_PORT`. |
| `PROD` | `0` | `1` runs a production build (`next build` + `next start`) |

Example with the backend on another port:

```bash
API_PORT=9000 scripts/start-backend.sh                      # terminal 1
API_URL=http://127.0.0.1:9000 scripts/start-ui.sh           # terminal 2
```

### Step 7: Use the platform

| Page | What it shows |
|------|---------------|
| **Scenarios** (`/`) | Every harvested scenario: quad counts, SHACL status, as-of cutoff and live alert |
| **Scenario detail** (`/scenarios/<name>`) | The live symptom, ranked causal chains, changes near the alert, the dependency topology, and a button that starts **AutoRCA** (needs the Fireworks key) |
| **Case view** (`/scenarios/<name>/case`) | Why the AutoRCA verdict is the right root cause: a swimlane (time × service) or force graph of the run, suspects with computed facts beside the agent's own reasons, raw → processed → graph lineage on every node, the agent's trail, the eval scorecard, and what happened after the agent's cut-off |
| **SPARQL** (`/sparql`) | Read-only `SELECT` queries against the as-of or full store |
| **Ontology & taxonomy** (`/ontology`) | OWL classes and properties, and SKOS concept schemes |
| **Evals** (`/evals`) | Scored RCA eval runs from `data/evals/` |

The sidebar footer shows `API unreachable` when the backend is not running, and
`AutoRCA: no Fireworks key` when `.env` has no key.

#### The case view

The case view explains one saved run: by default the graph-arm run of the newest eval run in
`data/evals/`, or `?source=eval:<run>` / `?source=job:<id>` for another eval run or a finished
AutoRCA job. With no saved run it says which command makes one.

| Endpoint | What it returns |
|----------|-----------------|
| `GET /api/scenarios/<s>/case` | The case graph (`meta`, `nodes`, `edges`, `trail`), read from the **as-of** store, the one the agent saw. `meta.tool_issues` lists tool calls that failed or were cut to fit while building it. |
| `GET /api/scenarios/<s>/lineage?uri=` | The raw lines behind a node, with file, byte offset and hash, and the processed row each one became |
| `GET /api/scenarios/<s>/scorecard` | Every arm of the eval run (LLM agent arms and correlation engine arms), and this scenario's result per arm |
| `GET /api/scenarios/<s>/after` | Metrics and changes **after** the agent's cut-off, from the full store, always labelled as such |

- **Trail.** AutoRCA jobs and eval runs save the agent's trail next to the report: every
  finding the specialists recorded (claim, confidence, cited items) and every tool call in order.
  Runs from before the trail existed say so instead of showing an empty panel.
- **Recovery after the fix.** A scenario whose fault has a fix recovers after it, so the after
  panel shows the failed mitigations and then the metrics coming back. The fix only changes
  records after the cut-off: every record at or before it, the agent's input, is generated as
  without the fix.
- **As-of log templates.** The as-of view mines log templates from the log lines observable at
  the cut-off only, so later lines cannot reshape what the agent saw. Its template ids and texts
  can therefore differ from the full store's.

---

## Command line (optional)

The scripts wrap the `e2ekg` CLI. You can run each stage yourself:

```bash
uv run e2ekg generate   --scenario hero                 # raw MELT data
uv run e2ekg preprocess --scenario hero --llm scripted  # lossless parquet
uv run e2ekg ledger     --scenario hero                 # coverage proofs
uv run e2ekg harvest    --scenario hero                 # RDF store (full)
uv run e2ekg harvest    --scenario hero --as-of         # RDF store (as of alert + 30 min)
uv run e2ekg rca  --scenario hero --alert radar-09000   # AutoRCA (needs FIREWORKS_API_KEY)
uv run e2ekg eval --scenarios all --runs 3              # score the RCA arms
uv run e2ekg ontology propose                           # LLM-proposed ontology extensions, test-gated
uv run e2ekg --help
```

Run the tests:

```bash
uv run pytest                          # fast suite (live and slow tests excluded)
uv run pytest tests/ontology           # SHACL shapes and competency questions
```

---

## Repository layout

```
scripts/
  ingest.sh          1. env + generate + preprocess + ledger + harvest (+ as-of) + ontology checks
  start-backend.sh   2. FastAPI on :8000 over the harvested stores (no ingestion)
  start-ui.sh        3. Next.js UI on :3000
src/e2ekg/
  scenario/ generate/      scenario model and messy-data generator
  preprocess/              lossless parsing, time normalization, identity, ledger
  graph/                   enrichers, harvest, Oxigraph store
  correlate/               causal-chain correlation engine
  agent/                   AutoRCA (DeepAgents) and its graph tools
  evals/ flywheel/         eval harness, approvals and learned patterns
  ontology_evolve/         LLM-proposed ontology extensions
  api/app.py               FastAPI service
ontology/
  core.ttl  shapes.ttl     OWL ontology and SHACL shapes
  schemes/                 SKOS taxonomies (severity, change kind, fault kind, ...)
  extensions/              accepted ontology extensions
  cq/                      competency questions (SPARQL + expected answers)
ui/                        Next.js app
data/                      generated (git-ignored): raw/, processed/, graph/, evals/
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `need uv ...` | Install uv (`brew install uv`) |
| `no harvested graph under data/graph` | Run `scripts/ingest.sh` |
| UI sidebar says **API unreachable** | Start the backend, and check that `API_URL` matches its host and port |
| `AutoRCA needs FIREWORKS_API_KEY` | Add the key to `.env` and restart the backend |
| `address already in use` | Another process holds the port. Use `API_PORT=...` or `PORT=...`, or stop it: `lsof -ti :8000 \| xargs kill` |
| Ledger reports `chat outputs missing` | You ran with `--llm none`. Re-run with `--llm scripted`. |
| You want to start from a clean slate | `scripts/ingest.sh --clean` |
| Disk is filling up | Ingest one scenario (`--scenario hero`) or a smaller dataset (`--scale 0.2 --skip-tests`) |
| `tests/ontology/test_cq.py` fails (CQ 06, "reproducible from the generator") | You generated with `--scale` other than `1.0`. Add `--skip-tests`, or regenerate at full scale with `--clean`. |
