#!/usr/bin/env bash
# turboquant-upgrade.sh - Detect when stable Ollama (or its bundled MLX)
# gains real TurboQuant tq3/tq4 KV-cache support, and flip our config to
# use it. Until that day, this script reports the upstream status and
# leaves the live stack on the strongest stable type (q4_0).
#
# Background:
#   - ollama/ollama#15090 (the Go-native TurboQuant PR) was abandoned
#     Apr 2026; maintainers chose to wait for MLX upstream.
#   - ml-explore/mlx#3328 implements TurboQuant inside MLX core.
#   - Once MLX merges + Ollama bumps its MLX dependency, `tq3`/`tq4` will
#     appear as recognized strings in the ollama binary. That's our trigger.
#
# Usage:
#   bash scripts/turboquant-upgrade.sh           # one-shot check + report
#   bash scripts/turboquant-upgrade.sh --apply   # check, and if supported, flip
#   bash scripts/turboquant-upgrade.sh --watch   # poll daily, apply when ready
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO_ROOT/config/detected.env"

MODE="${1:-report}"
case "$MODE" in
  --apply|apply) MODE="apply" ;;
  --watch|watch) MODE="watch" ;;
  *)             MODE="report" ;;
esac

log()  { printf "\033[1;34m[turboquant]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[turboquant]\033[0m %s\n" "$*" >&2; }
ok()   { printf "\033[1;32m[turboquant]\033[0m %s\n" "$*"; }

OLLAMA_BIN="$(command -v ollama || echo /opt/homebrew/bin/ollama)"

probe_supported_types() {
  if [[ ! -x "$OLLAMA_BIN" ]]; then
    echo ""; return
  fi
  strings "$OLLAMA_BIN" 2>/dev/null \
    | grep -Eo '\b(tq3|tq4|q4_0|q8_0|f16)\b' | sort -u | tr '\n' ' '
}

probe_ollama_version() {
  ollama --version 2>/dev/null | awk '{print $NF}' | tail -1
}

upstream_status() {
  cat <<'EOF'
Upstream tracking:
  Ollama PR:  https://github.com/ollama/ollama/pull/15090   (CLOSED Apr 2026)
  Ollama PR:  https://github.com/ollama/ollama/pull/15125   (engine-wiring follow-up)
  MLX PR:     https://github.com/ml-explore/mlx/pull/3328   (TurboQuant in MLX core)
  Paper:      arxiv 2504.19874 (TurboQuant, ICLR 2026)
  llama.cpp:  PR #21131 (--turbo-kv flag, working on CPU/CUDA/HIP)

Trigger condition:
  When `strings $(which ollama) | grep -E 'tq3|tq4'` returns a match,
  the daemon recognizes TurboQuant and we can flip OLLAMA_KV_CACHE_TYPE.
EOF
}

apply_kv() {
  local kv="$1"
  if [[ "$KV_CACHE_TYPE" == "$kv" ]]; then
    ok "config/detected.env already KV_CACHE_TYPE=$kv; nothing to do."
    return
  fi
  log "rewriting config/detected.env: $KV_CACHE_TYPE -> $kv"
  sed -i.bak "s|^KV_CACHE_TYPE=.*|KV_CACHE_TYPE=\"$kv\"|" "$REPO_ROOT/config/detected.env"
  rm -f "$REPO_ROOT/config/detected.env.bak"
  log "re-rendering plists"
  bash "$REPO_ROOT/scripts/60-dashboard.sh" >/dev/null
  log "bouncing Ollama"
  launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.local.ollama.plist" 2>/dev/null || true
  sleep 2
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.local.ollama.plist"
  sleep 3
  ok "applied $kv. Run \`make verify\` to confirm."
}

run_check() {
  local supported types kv version
  supported="$(probe_supported_types)"
  version="$(probe_ollama_version)"
  log "ollama binary:    $OLLAMA_BIN"
  log "ollama version:   ${version:-unknown}"
  log "currently using:  KV_CACHE_TYPE=$KV_CACHE_TYPE (from config/detected.env)"
  log "daemon supports:  ${supported:-<none detected>}"

  if [[ " $supported " == *" tq3 "* ]] || [[ " $supported " == *" tq4 "* ]]; then
    ok "TurboQuant tq3/tq4 IS now supported by this Ollama build."
    if [[ "$MODE" == "apply" || "$MODE" == "watch" ]]; then
      # Prefer tq3 (paper-default 3-bit), fall back to tq4 if only that is in.
      if [[ " $supported " == *" tq3 "* ]]; then
        apply_kv "tq3"
      else
        apply_kv "tq4"
      fi
      return 0
    else
      log "Re-run with --apply to flip live, or \`make turboquant-upgrade-apply\`."
      return 0
    fi
  fi

  warn "TurboQuant not yet available in this Ollama build."
  upstream_status
  return 1
}

case "$MODE" in
  report|apply)
    run_check || exit 0  # not-yet-supported is informational, not a failure
    ;;
  watch)
    log "watch mode: checking once a day until tq3 is supported. Ctrl-C to stop."
    while true; do
      if run_check; then
        ok "watch loop exiting cleanly."
        break
      fi
      log "next check in 24h ($(date -v+1d 2>/dev/null || date -d '+1 day' 2>/dev/null))"
      sleep 86400
    done
    ;;
esac
