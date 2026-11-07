#!/usr/bin/env bash
# 2/3 Start the e2ekg API (FastAPI over the already-ingested Oxigraph stores; never ingests).
#   scripts/start-backend.sh                  # http://127.0.0.1:8000 (docs at /docs)
#   API_PORT=9000 scripts/start-backend.sh    # other port (then start the UI with API_URL=...)
#   RELOAD=1 scripts/start-backend.sh         # auto-reload on code changes
#   E2EKG_DATA=/path/to/data scripts/start-backend.sh
# FIREWORKS_API_KEY (from .env or the environment) enables AutoRCA runs; without it the
# graph, chains, SPARQL, ontology and eval views still work.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HOST="${API_HOST:-127.0.0.1}"
PORT="${API_PORT:-8000}"
export E2EKG_DATA="${E2EKG_DATA:-$ROOT/data}"

if [[ -f .env ]]; then set -a; source .env; set +a; fi

if command -v uv >/dev/null 2>&1; then
  uv sync -q
  PY=(uv run python)
elif [[ -x .venv/bin/python ]]; then
  PY=(.venv/bin/python)
  if ! "${PY[@]}" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
    echo ">> installing fastapi + uvicorn into .venv (install uv to manage deps from uv.lock)"
    "${PY[@]}" -m ensurepip >/dev/null 2>&1 || true
    "${PY[@]}" -m pip install -q "fastapi>=0.115" "uvicorn>=0.32"
  fi
else
  echo "need uv (https://docs.astral.sh/uv/) or run scripts/ingest.sh first" >&2; exit 1
fi

if [[ ! -d "$E2EKG_DATA/graph" ]]; then
  echo "!! no harvested graph under $E2EKG_DATA/graph; run scripts/ingest.sh first" >&2
fi
if [[ -n "${FIREWORKS_API_KEY:-}" ]]; then
  echo ">> AutoRCA: enabled (Fireworks)"
else
  echo ">> AutoRCA: disabled (set FIREWORKS_API_KEY in .env to enable)"
fi
echo ">> API on http://$HOST:$PORT   (docs: http://$HOST:$PORT/docs, data: $E2EKG_DATA)"

ARGS=(e2ekg.api.app:app --host "$HOST" --port "$PORT")
[[ "${RELOAD:-0}" == 1 ]] && ARGS+=(--reload --reload-dir src/e2ekg)
exec "${PY[@]}" -m uvicorn "${ARGS[@]}"
