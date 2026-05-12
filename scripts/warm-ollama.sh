#!/usr/bin/env bash
# warm-ollama.sh - Pre-load the long-context Ollama model so the first
# Cline / Cursor turn doesn't pay the ~50s model-load cold start.
#
# What it does:
#   1. Waits for Ollama's /api/tags endpoint to come up (max WARM_WAIT_S).
#   2. Checks /api/ps -- if the target model is already loaded, exits 0.
#   3. POSTs an empty-prompt /api/generate request with `keep_alive: -1`,
#      which tells Ollama to load the model and never unload it. The
#      request itself produces zero output tokens so it's near-free.
#
# This script is invoked automatically:
#   - By the launchd agent `com.local.ollama-warm` after Ollama starts.
#   - By `make warm` for manual / verification use.
#
# It is intentionally idempotent and safe to call repeatedly: an
# already-loaded model just gets its keep-alive refreshed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
[ -f "$REPO_ROOT/config/detected.env" ] && source "$REPO_ROOT/config/detected.env"

OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:${OLLAMA_PORT:-11434}}"
MODEL="${1:-${OLLAMA_TAG:-qwen3-coder-next:q4_K_M}}"
WARM_WAIT_S="${WARM_WAIT_S:-120}"

log() { printf "\033[1;33m[warm-ollama]\033[0m %s\n" "$*"; }

# Wait for Ollama to be reachable.
deadline=$(( $(date +%s) + WARM_WAIT_S ))
while ! curl -fsS "http://${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; do
  if (( $(date +%s) >= deadline )); then
    log "Ollama at ${OLLAMA_HOST} not reachable after ${WARM_WAIT_S}s; aborting"
    exit 1
  fi
  sleep 2
done

# Is the model already running?
if curl -fsS "http://${OLLAMA_HOST}/api/ps" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(any(m.get('name','')==sys.argv[1] for m in d.get('models',[])))" "$MODEL" \
    2>/dev/null | grep -q True; then
  log "model ${MODEL} already loaded -- refreshing keep_alive"
fi

log "warming ${MODEL} (this can take 30-60s on cold cache)..."

# keep_alive: -1 keeps the model loaded indefinitely.
# An empty prompt yields ~0 output tokens, so this is the cheapest
# possible model-load trigger.
response="$(curl -fsS -m "$WARM_WAIT_S" "http://${OLLAMA_HOST}/api/generate" \
  -H 'Content-Type: application/json' \
  -d "$(printf '{"model":"%s","prompt":"","keep_alive":-1,"options":{"num_predict":1}}' "$MODEL")" \
  || true)"

if [[ -z "$response" ]]; then
  log "warmup request failed or timed out"
  exit 1
fi

log "ok - ${MODEL} is hot"
