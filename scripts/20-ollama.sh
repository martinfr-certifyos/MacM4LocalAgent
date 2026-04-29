#!/usr/bin/env bash
# 20-ollama.sh - Configure Ollama with TurboQuant KV cache and pull the model.
# Set OLLAMA_FROM_SOURCE=1 to build the TurboQuant branch from source if brew lags.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO_ROOT/config/detected.env"

log() { printf "\033[1;34m[ollama]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[ollama]\033[0m %s\n" "$*" >&2; }

if ! command -v ollama >/dev/null 2>&1; then
  echo "ollama not on PATH; run scripts/10-brew.sh first" >&2
  exit 1
fi

log "Setting TurboQuant KV cache env (persistent via launchctl):"
log "  OLLAMA_KV_CACHE_TYPE=$KV_CACHE_TYPE"
log "  OLLAMA_FLASH_ATTENTION=1"
log "  OLLAMA_NUM_PARALLEL=2"
launchctl setenv OLLAMA_KV_CACHE_TYPE "$KV_CACHE_TYPE"
launchctl setenv OLLAMA_FLASH_ATTENTION 1
launchctl setenv OLLAMA_NUM_PARALLEL 2
launchctl setenv OLLAMA_HOST "127.0.0.1:${OLLAMA_PORT}"

# Optional: build from source if brew Ollama predates the TurboQuant PR.
if [[ "${OLLAMA_FROM_SOURCE:-0}" == "1" ]]; then
  log "OLLAMA_FROM_SOURCE=1 - building Ollama with TurboQuant from source"
  if ! command -v go >/dev/null 2>&1; then brew install go; fi
  SRC="$REPO_ROOT/.build/ollama"
  mkdir -p "$REPO_ROOT/.build"
  if [[ ! -d "$SRC" ]]; then
    git clone https://github.com/ollama/ollama "$SRC"
  fi
  ( cd "$SRC" && git fetch --all && git checkout main && git pull && go build -o "$REPO_ROOT/.build/ollama-bin" . )
  log "built $REPO_ROOT/.build/ollama-bin (use this binary in launchd plist if needed)"
fi

# Probe whether the running Ollama supports TurboQuant by inspecting the version string.
OLLAMA_VER="$(ollama --version 2>/dev/null | awk '{print $NF}' | head -1 || echo unknown)"
log "ollama version: $OLLAMA_VER"

# Start a temporary daemon if one isn't already listening, just to pull the model.
ALREADY_RUNNING=0
if curl -fsS "http://127.0.0.1:${OLLAMA_PORT}/api/version" >/dev/null 2>&1; then
  ALREADY_RUNNING=1
  log "ollama daemon already running"
else
  log "starting transient ollama daemon for model pull"
  OLLAMA_KV_CACHE_TYPE="$KV_CACHE_TYPE" OLLAMA_FLASH_ATTENTION=1 \
    nohup ollama serve >/tmp/ollama-bootstrap.log 2>&1 &
  OLLAMA_PID=$!
  for i in {1..30}; do
    sleep 1
    if curl -fsS "http://127.0.0.1:${OLLAMA_PORT}/api/version" >/dev/null 2>&1; then
      break
    fi
  done
fi

# Pull the chosen model.
#
# Source preference:
# 1. HF (Hugging Face, US-hosted via Cloudfront LAX) using hf_transfer with
#    8 parallel connections. Aggregate throughput is ~8-10 MB/s in our
#    testing, vs ~1 MB/s from registry.ollama.ai (Cloudflare's per-connection
#    throttle on the Ollama-hosted blobs). This is the default path.
# 2. registry.ollama.ai via `ollama pull` as a fallback. Set
#    OLLAMA_SOURCE=registry to skip HF entirely.
#
# To use HF you also need the MLX venv populated (scripts/30-mlx.sh). The
# helper `scripts/download-ollama-from-hf.sh` uses huggingface_hub there and
# then `ollama create` to register the GGUF locally.
OLLAMA_SOURCE="${OLLAMA_SOURCE:-hf}"

if [[ "$OLLAMA_SOURCE" == "hf" ]] && [[ -x "$REPO_ROOT/scripts/download-ollama-from-hf.sh" ]] && [[ -x "$REPO_ROOT/.venvs/mlx/bin/python" ]]; then
  log "preferred path: HF (hf_transfer, 8 parallel streams)"
  HF_REPO="${HF_REPO:-bartowski/Qwen_Qwen3-Coder-Next-GGUF}"
  case "$QUANT_TIER" in
    q8) HF_FILE="${HF_FILE:-Qwen_Qwen3-Coder-Next-Q8_0.gguf}" ;;
    *)  HF_FILE="${HF_FILE:-Qwen_Qwen3-Coder-Next-Q4_K_M.gguf}" ;;
  esac
  HF_TAG="${HF_TAG:-qwen3-coder-next:${QUANT_TIER:-q4_K_M}}"

  if bash "$REPO_ROOT/scripts/download-ollama-from-hf.sh" "$HF_REPO" "$HF_FILE" "$HF_TAG"; then
    PULLED="$HF_TAG"
    log "HF path succeeded; tag=$PULLED"
  else
    warn "HF path failed; falling back to ollama registry"
    OLLAMA_SOURCE="registry"
  fi
fi

if [[ "$OLLAMA_SOURCE" == "registry" ]] || [[ -z "${PULLED:-}" ]]; then
# Retry policy for registry path:
# - Each candidate gets PULL_RETRIES quick attempts.
# - Ollama writes partial blobs as sparse files in ~/.ollama/models/blobs/
#   named sha256-<digest>-partial. Subsequent `ollama pull <same-tag>` calls
#   resume from those partials; the data is NOT thrown away on failure.
# - Therefore, if a candidate has any partial blob with substantial allocated
#   progress (>10% of its apparent size), we DO NOT fall through to a
#   different candidate. We keep retrying the same tag forever (with a back-
#   off) so we don't waste hours of bandwidth re-downloading a different model.
#   Override with PULL_FALLTHROUGH=1 to force the old behavior.
CANDIDATES=(
  "$OLLAMA_TAG"
  "qwen3-coder-next:q4_K_M"
  "qwen3-coder-next:latest"
  "qwen3-coder:30b-a3b-q8_0"
  "qwen3-coder:30b"
)
PULL_RETRIES="${PULL_RETRIES:-3}"
PULL_FALLTHROUGH="${PULL_FALLTHROUGH:-0}"
PULL_RESUME_BACKOFF="${PULL_RESUME_BACKOFF:-30}"

has_substantial_partial_progress() {
  # Are any *.partial blobs at least 10% allocated? If yes, return 0 (true).
  # We can't precisely attribute partials to a specific tag without the
  # manifest, but for our use case (one tag at a time) it's a safe heuristic.
  local blobs_dir="$HOME/.ollama/models/blobs"
  [[ -d "$blobs_dir" ]] || return 1
  local f apparent allocated
  for f in "$blobs_dir"/sha256-*-partial; do
    [[ -e "$f" ]] || continue
    [[ "$f" == *"-partial-"* ]] && continue
    apparent=$(stat -f "%z" "$f" 2>/dev/null || echo 0)
    allocated=$(($(stat -f "%b" "$f" 2>/dev/null || echo 0) * 512))
    (( apparent > 0 )) || continue
    if (( allocated * 10 > apparent )); then
      return 0
    fi
  done
  return 1
}

  for tag in "${CANDIDATES[@]}"; do
    attempt=0
    while :; do
      attempt=$((attempt + 1))
      log "trying to pull $tag (attempt $attempt)"
      if ollama pull "$tag"; then
        PULLED="$tag"
        break 2
      fi
      warn "pull failed for $tag (attempt $attempt)"

      if (( PULL_FALLTHROUGH == 0 )) && has_substantial_partial_progress; then
        warn "substantial partial progress detected; resuming '$tag' rather than falling through"
        warn "(set PULL_FALLTHROUGH=1 to override; sleeping ${PULL_RESUME_BACKOFF}s)"
        sleep "$PULL_RESUME_BACKOFF"
        continue
      fi

      if (( attempt >= PULL_RETRIES )); then
        warn "PULL_RETRIES=$PULL_RETRIES exhausted for $tag, trying next candidate"
        break
      fi
      sleep 5
    done
  done
fi  # end registry-fallback block

if [[ -z "${PULLED:-}" ]]; then
  warn "no model could be pulled; you can pull one manually with: ollama pull qwen3-coder:30b"
else
  log "pulled $PULLED"
  if [[ "$PULLED" != "$OLLAMA_TAG" ]]; then
    sed -i.bak "s|^OLLAMA_TAG=.*|OLLAMA_TAG=\"$PULLED\"|" "$REPO_ROOT/config/detected.env"
    rm -f "$REPO_ROOT/config/detected.env.bak"
    log "updated config/detected.env OLLAMA_TAG=$PULLED"
  fi
fi

# Stop the transient daemon (launchd will own it later).
if (( ALREADY_RUNNING == 0 )); then
  kill "${OLLAMA_PID:-0}" 2>/dev/null || true
fi

log "done"
