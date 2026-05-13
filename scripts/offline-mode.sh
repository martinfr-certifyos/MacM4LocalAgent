#!/usr/bin/env bash
# Toggle OFFLINE / OFFLINE_STRICT in config/detected.env, and print
# the live network-probe result. The router re-reads detected.env on
# every routing decision, so flipping this flag takes effect on the
# NEXT request -- no proxy restart needed.
#
# Usage:
#   scripts/offline-mode.sh on            # force OFFLINE=1
#   scripts/offline-mode.sh off           # set OFFLINE=auto (probe drives it)
#   scripts/offline-mode.sh status        # show current state + live probe
#   scripts/offline-mode.sh strict on     # set OFFLINE_STRICT=1
#   scripts/offline-mode.sh strict off    # set OFFLINE_STRICT=0

set -eu -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/config/detected.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "error: $ENV_FILE not found. Run \`make detect\` first." >&2
  exit 1
fi

upsert_kv() {
  local key="$1"
  local value="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    # macOS sed in-place needs the '' arg after -i.
    sed -i '' -E "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

get_kv() {
  local key="$1"
  grep -E "^${key}=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true
}

probe_anthropic() {
  # 1.5s TCP-connect probe; matches router/offline_mode.py exactly so
  # the script's "live probe" line agrees with what the router sees.
  python3 - <<'PY'
import socket, sys
try:
    s = socket.create_connection(("api.anthropic.com", 443), timeout=1.5)
    s.close()
    print("ONLINE")
except Exception as e:
    print(f"OFFLINE ({type(e).__name__}: {e})")
PY
}

cmd="${1:-status}"
case "$cmd" in
  on)
    upsert_kv "OFFLINE" "1"
    echo "OFFLINE=1 written to $ENV_FILE."
    echo "Router will downgrade Claude requests to local-long on the next call."
    ;;
  off)
    upsert_kv "OFFLINE" "auto"
    echo "OFFLINE=auto written to $ENV_FILE."
    echo "Router will auto-detect by probing api.anthropic.com:443."
    ;;
  strict)
    sub="${2:-}"
    case "$sub" in
      on)  upsert_kv "OFFLINE_STRICT" "1"
           echo "OFFLINE_STRICT=1: explicit Claude requests will be rejected with 503 while offline." ;;
      off) upsert_kv "OFFLINE_STRICT" "0"
           echo "OFFLINE_STRICT=0: explicit Claude requests will be silently downgraded while offline." ;;
      *)   echo "usage: $0 strict on|off" >&2; exit 2 ;;
    esac
    ;;
  status)
    flag="$(get_kv OFFLINE)"
    strict="$(get_kv OFFLINE_STRICT)"
    flag="${flag:-<unset, treated as auto>}"
    strict="${strict:-<unset, treated as 0>}"
    echo "OFFLINE=${flag}"
    echo "OFFLINE_STRICT=${strict}"
    echo "live probe: $(probe_anthropic)"
    ;;
  *)
    cat >&2 <<EOF
usage: $0 <command>

commands:
  on              force OFFLINE=1 (skip probe, route Claude to local-long)
  off             OFFLINE=auto (probe api.anthropic.com:443 on each turn)
  strict on|off   toggle OFFLINE_STRICT (raise 503 instead of silent downgrade)
  status          show current flags + live probe result

The router re-reads $ENV_FILE on every routing decision. No proxy
restart is required. See docs/offline-mode.md for details.
EOF
    exit 2
    ;;
esac
