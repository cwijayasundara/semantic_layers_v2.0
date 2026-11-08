#!/usr/bin/env bash
# 3/3 Start the Next.js UI. The browser talks only to Next, which proxies /api/* to the backend.
#   scripts/start-ui.sh                              # dev server on http://localhost:3000
#   PROD=1 scripts/start-ui.sh                       # production build + start
#   PORT=3100 scripts/start-ui.sh                    # other port
#   API_URL=http://127.0.0.1:9000 scripts/start-ui.sh   # backend elsewhere
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/ui"

command -v node >/dev/null || { echo "node not found (Node 20+ required)" >&2; exit 1; }
command -v npm  >/dev/null || { echo "npm not found" >&2; exit 1; }
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
[[ "$NODE_MAJOR" -ge 20 ]] || { echo "Node 20+ required (found $(node -v))" >&2; exit 1; }

export API_URL="${API_URL:-http://127.0.0.1:8000}"
PORT="${PORT:-3000}"

if [[ ! -d node_modules || package.json -nt node_modules/.package-lock.json ]]; then
  echo ">> installing UI dependencies"
  npm install --no-audit --no-fund
fi

if ! curl -fsS -m 2 "$API_URL/api/status" >/dev/null 2>&1; then
  echo "!! backend not reachable at $API_URL; start it with scripts/start-backend.sh (the UI connects once it is up)"
fi

echo ">> UI on http://localhost:$PORT   (API proxied to $API_URL)"
if [[ "${PROD:-0}" == 1 ]]; then
  npm run build
  exec npx next start -p "$PORT"
fi
exec npx next dev -p "$PORT"
