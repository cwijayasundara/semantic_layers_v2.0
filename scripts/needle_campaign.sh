#!/usr/bin/env bash
# Needle-in-haystack campaign: density sweep (scale 4/16), attribution ablation
# (none), live-chat arm (fireworks preprocess+harvest). Builds then evals.
set -uo pipefail
cd /Users/cwijayasundaragmail.com/Documents/learning_103/semantic_layers_v2.0
set -a; source .env; set +a

E2EKG="uv run e2ekg"
NAMES=$(uv run python -c "
from e2ekg.scenario.hero import hero_scenario
from e2ekg.scenario.variants import variant_scenarios
print(' '.join(s.name for s in (hero_scenario(7), *variant_scenarios(7))))")
step() { printf '\n==> %s [%s]\n' "$*" "$(date +%H:%M:%S)"; }

build_root() {  # ROOT SCALE ATTR
  local ROOT=$1 SCALE=$2 ATTR=$3
  step "generate scale=$SCALE attribution=$ATTR -> $ROOT/raw"
  for name in $NAMES; do
    if [[ -d "$ROOT/raw/$name" && -n "$(ls -A "$ROOT/raw/$name" 2>/dev/null)" ]]; then
      echo "$name: already generated"
    else
      $E2EKG generate --scenario "$name" --seed 7 --scale "$SCALE" \
        --attribution "$ATTR" --out "$ROOT/raw" || return 1
    fi
  done
  step "preprocess+harvest as-of -> $ROOT"
  $E2EKG preprocess --scenario all --raw "$ROOT/raw" --out "$ROOT/processed" \
    --llm scripted || return 1
  $E2EKG harvest --scenario all --proc "$ROOT/processed" --out "$ROOT/graph" \
    --as-of || return 1
}

build_root data-exp/s4 4 guess  || exit 1
build_root data-exp/s8 8 guess || exit 1
build_root data-exp/nattr 1 none || exit 1

step "chatlive root: reuse data/raw, preprocess+harvest with live LLM"
mkdir -p data-exp/chatlive
ln -sfn "$(pwd)/data/raw" data-exp/chatlive/raw
$E2EKG preprocess --scenario all --raw data/raw --out data-exp/chatlive/processed \
  --llm fireworks || exit 1
$E2EKG harvest --scenario all --proc data-exp/chatlive/processed \
  --out data-exp/chatlive/graph --llm fireworks --as-of || exit 1

step "evals"
$E2EKG eval --root data-exp/s4 --scenarios all --arms graph,raw_only --runs 1 \
  --run-dir data-exp/s4/evals/sweep || exit 1
$E2EKG eval --root data-exp/s8 --scenarios all --arms graph,raw_only --runs 1 \
  --run-dir data-exp/s8/evals/sweep || exit 1
$E2EKG eval --root data-exp/nattr --scenarios all --arms graph --runs 3 \
  --run-dir data-exp/nattr/evals/attr-none || exit 1
$E2EKG eval --root data-exp/chatlive --scenarios all --arms graph --runs 3 \
  --run-dir data-exp/chatlive/evals/chat-live || exit 1

step "CAMPAIGN COMPLETE"
