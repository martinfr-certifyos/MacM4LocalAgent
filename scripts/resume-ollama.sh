#!/usr/bin/env bash
# resume-ollama.sh - Keep `ollama pull <tag>` alive across transient TCP resets.
#
# Reasons this exists:
# - The model is huge (~84 GB for q8_0). On flaky IPv6 paths to
#   registry.ollama.ai, Cloudflare (2606:4700:2ff9::1) frequently resets
#   long-lived TLS connections.
# - Ollama's CLI gives up after a small number of internal retries.
# - But its on-disk format is fully resumable: each shard is a sparse partial
#   blob in ~/.ollama/models/blobs/sha256-*-partial, and a fresh
#   `ollama pull <same-tag>` picks up exactly where the last attempt left off.
#
# So: just run it in a loop with backoff. Stop when it succeeds OR when the
# allocated bytes haven't grown for STALL_WINDOW seconds (truly hung, not
# transiently failing).
#
# Usage:
#   bash scripts/resume-ollama.sh [tag]
#
# Defaults to OLLAMA_TAG from config/detected.env.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO_ROOT/config/detected.env"

TAG="${1:-$OLLAMA_TAG}"
LOG_DIR="$REPO_ROOT/.logs"
LOG="$LOG_DIR/install-ollama-resume-${TAG//[:\/]/_}.log"
BLOBS="$HOME/.ollama/models/blobs"

BACKOFF_BASE="${BACKOFF_BASE:-15}"
BACKOFF_MAX="${BACKOFF_MAX:-180}"
STALL_WINDOW="${STALL_WINDOW:-1800}"   # 30 minutes
MAX_ATTEMPTS="${MAX_ATTEMPTS:-200}"

mkdir -p "$LOG_DIR"

log() { printf "\033[1;34m[resume-ollama]\033[0m %s\n" "$*" | tee -a "$LOG"; }
warn() { printf "\033[1;33m[resume-ollama]\033[0m %s\n" "$*" | tee -a "$LOG" >&2; }

# Sum of allocated bytes across all *.partial blobs (excluding -partial-N
# per-shard meta files). Returns the number on stdout.
total_allocated_bytes() {
  [[ -d "$BLOBS" ]] || { echo 0; return; }
  local sum=0 b alloc
  for b in "$BLOBS"/sha256-*-partial; do
    [[ -e "$b" ]] || continue
    [[ "$b" == *"-partial-"* ]] && continue
    alloc=$(($(stat -f "%b" "$b" 2>/dev/null || echo 0) * 512))
    sum=$(( sum + alloc ))
  done
  echo "$sum"
}

log "starting resume loop for tag=$TAG"
log "log file: $LOG"

last_size=$(total_allocated_bytes)
last_change=$(date +%s)
attempt=0

while (( attempt < MAX_ATTEMPTS )); do
  attempt=$(( attempt + 1 ))
  log "attempt $attempt: ollama pull $TAG"

  if ollama pull "$TAG" 2>&1 | tee -a "$LOG"; then
    log "SUCCESS - pulled $TAG"
    log "verifying with ollama list..."
    ollama list | tee -a "$LOG"
    exit 0
  fi

  rc=$?
  warn "attempt $attempt failed (exit $rc)"

  current_size=$(total_allocated_bytes)
  if (( current_size > last_size )); then
    delta_gb=$(awk -v c="$current_size" -v l="$last_size" 'BEGIN { printf "%.2f", (c-l)/1024/1024/1024 }')
    log "good news: +${delta_gb} GB allocated since last check; partials are growing"
    last_size=$current_size
    last_change=$(date +%s)
  else
    age=$(( $(date +%s) - last_change ))
    if (( age > STALL_WINDOW )); then
      warn "no on-disk progress for ${age}s (> ${STALL_WINDOW}s STALL_WINDOW). Aborting resume loop."
      warn "Investigate: ollama logs, network, registry status. Then re-run: bash scripts/resume-ollama.sh"
      exit 2
    fi
    log "no allocation change yet (${age}s since last forward progress; will keep retrying)"
  fi

  # Exponential backoff capped at BACKOFF_MAX.
  sleep_for=$(( BACKOFF_BASE * (2 ** (attempt > 6 ? 6 : attempt - 1)) ))
  if (( sleep_for > BACKOFF_MAX )); then sleep_for=$BACKOFF_MAX; fi
  log "sleeping ${sleep_for}s before retry"
  sleep "$sleep_for"
done

warn "MAX_ATTEMPTS=$MAX_ATTEMPTS reached without success"
exit 3
