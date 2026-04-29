#!/usr/bin/env bash
# 10-brew.sh - Install Homebrew dependencies (idempotent).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
log() { printf "\033[1;34m[brew]\033[0m %s\n" "$*"; }

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required. Install it from https://brew.sh first." >&2
  exit 1
fi

# Formulas needed by the installer.
FORMULAS=(ollama jq sqlite uv llama.cpp)

for f in "${FORMULAS[@]}"; do
  if brew list --formula "$f" >/dev/null 2>&1; then
    log "$f already installed"
  else
    log "installing $f"
    brew install "$f"
  fi
done

# Make sure brew shellenv is loaded for the rest of this run.
eval "$(brew shellenv)"

log "Versions:"
ollama --version 2>/dev/null | head -1 | sed 's/^/  ollama:   /'
uv --version       | sed 's/^/  uv:       /'
sqlite3 --version  | awk '{print "  sqlite3:  " $1}'
jq --version       | sed 's/^/  jq:       /'
log "done"
