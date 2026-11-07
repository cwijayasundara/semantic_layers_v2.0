#!/usr/bin/env bash
# 1/3 Ingest: set up the Python env, generate raw MELT data, preprocess losslessly, prove
# the ledger, and load the RDF knowledge graph (ontology + taxonomy schemes + data) into
# Oxigraph: both the full history store and the as-of store the RCA agent reads.
# Run this once (or when you want to rebuild); the backend never re-ingests.
#
# Usage: scripts/ingest.sh [--scenario hero|v-...|all] [--llm scripted|fireworks|none]
#                         [--seed N] [--scale X] [--clean] [--skip-tests]
# Defaults: --scenario all --llm scripted (no network). --llm fireworks needs
# FIREWORKS_API_KEY (read from .env if present).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SCENARIO=all LLM=scripted SEED=7 SCALE=1.0 CLEAN=0 TESTS=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --scenario) SCENARIO="$2"; shift 2 ;;
    --llm) LLM="$2"; shift 2 ;;
    --seed) SEED="$2"; shift 2 ;;
    --scale) SCALE="$2"; shift 2 ;;
    --clean) CLEAN=1; shift ;;
    --skip-tests) TESTS=0; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }

if [[ -f .env ]]; then set -a; source .env; set +a; fi
if [[ "$LLM" == fireworks && -z "${FIREWORKS_API_KEY:-}" ]]; then
  echo "--llm fireworks needs FIREWORKS_API_KEY (in the environment or .env)" >&2; exit 1
fi

step "Python environment"
if command -v uv >/dev/null 2>&1; then
  uv sync
  RUN=(uv run)
elif [[ -x .venv/bin/python ]]; then
  echo "uv not found; using existing .venv (install uv to refresh dependencies)"
  RUN=(.venv/bin/python -m)
  PY=(.venv/bin/python)
  E2EKG=(.venv/bin/e2ekg)
else
  echo "need uv (https://docs.astral.sh/uv/) or an existing .venv" >&2; exit 1
fi
if [[ ${RUN[0]} == uv ]]; then E2EKG=(uv run e2ekg); PY=(uv run python); fi

if [[ $CLEAN == 1 ]]; then
  step "Removing previous data (keeping data/evals)"
  rm -rf data/raw data/processed data/graph
fi

step "Generate raw MELT data (scenario=$SCENARIO seed=$SEED scale=$SCALE)"
# The generator refuses a non-empty output dir, so generate only the scenarios not yet on
# disk; --clean regenerates them all. Later steps overwrite their own outputs.
if [[ "$SCENARIO" == all ]]; then
  NAMES=$("${PY[@]}" -c "
from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.variants import variant_scenarios
print(' '.join(s.name for s in (hero_scenario($SEED), *variant_scenarios($SEED))))")
else
  NAMES="$SCENARIO"
fi
for name in $NAMES; do
  if [[ -d "data/raw/$name" && -n "$(ls -A "data/raw/$name")" ]]; then
    echo "$name: already generated in data/raw/$name, skipping (--clean to regenerate)"
  else
    "${E2EKG[@]}" generate --scenario "$name" --seed "$SEED" --scale "$SCALE"
  fi
done

step "Preprocess: lossless parse, time normalization, identity, chat labels (llm=$LLM)"
"${E2EKG[@]}" preprocess --scenario "$SCENARIO" --llm "$LLM"

step "Ledger: byte coverage and fingerprint reachability"
LEDGER_ARGS=()
[[ "$LLM" == none ]] && LEDGER_ARGS+=(--allow-missing-chat)
"${E2EKG[@]}" ledger --scenario "$SCENARIO" "${LEDGER_ARGS[@]+"${LEDGER_ARGS[@]}"}"

# Harvest identity picks: fireworks if chosen, otherwise none (scripted is preprocess-only).
HARVEST_LLM=none
[[ "$LLM" == fireworks ]] && HARVEST_LLM=fireworks

step "Harvest: ontology + taxonomy schemes + data into the RDF store, SHACL validated"
"${E2EKG[@]}" harvest --scenario "$SCENARIO" --llm "$HARVEST_LLM"

step "Harvest the as-of store (what was observable at alert + 30 min) for the RCA agent"
"${E2EKG[@]}" harvest --scenario "$SCENARIO" --llm "$HARVEST_LLM" --as-of

if [[ $TESTS == 1 ]]; then
  step "Ontology checks: shapes and competency questions"
  "${RUN[@]}" pytest -q tests/ontology
fi

step "Done"
for d in data/graph/*/; do
  name="$(basename "$d")"
  [[ "$SCENARIO" != all && "$SCENARIO" != "$name" ]] && continue
  echo "  $name: store $d""store, as-of $d""as_of/store"
done
