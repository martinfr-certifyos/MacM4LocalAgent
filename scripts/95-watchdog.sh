#!/usr/bin/env bash
# 95-watchdog.sh — Periodic cleanup of orphaned LiteLLM tasks and Ollama
# generations that survived a dropped client connection (Cline disconnect,
# Cursor crash, laptop sleep/wake, etc.).
#
# Run modes:
#   One-shot (cron / launchd timer):  bash 95-watchdog.sh
#   Interactive status check:         bash 95-watchdog.sh --status
#
# What it does:
#   1. Checks Ollama /api/ps for models whose keep_alive has expired but
#      whose OS process is still consuming GPU VRAM (Ollama bug / very long
#      generation). Sends a flush request to release VRAM.
#   2. Checks LiteLLM /health and the cost DB for requests that have been
#      open longer than STALE_THRESHOLD_SEC. If found, restarts LiteLLM via
#      launchctl so the upstream Ollama socket is closed cleanly.
#   3. Logs actions to WATCHDOG_LOG with timestamps.
#
# Design choice: restart LiteLLM (not kill Ollama) for stale tasks.
# Killing Ollama mid-generation leaves it in an undefined state; restarting
# the LiteLLM proxy closes the HTTP/2 stream that drives Ollama's generator,
# which causes Ollama to stop decoding within one token batch (~200 ms).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WATCHDOG_LOG="${REPO_ROOT}/.logs/watchdog.log"
OLLAMA_URL="http://127.0.0.1:11434"
LITELLM_URL="http://127.0.0.1:4000"
LITELLM_PLIST="com.local.litellm"
# A request is considered stale if it has been open longer than this.
# Must be > macm4 provider client-side timeout (300 s) and < litellm
# config request_timeout (360 s). We set 420 s here so the config timeout
# fires first; the watchdog is a belt-and-suspenders backstop.
STALE_THRESHOLD_SEC=420

STATUS_ONLY=false
[[ "${1:-}" == "--status" ]] && STATUS_ONLY=true

mkdir -p "$(dirname "$WATCHDOG_LOG")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$WATCHDOG_LOG"; }
ok()  { log "OK   $*"; }
warn(){ log "WARN $*"; }
act() { log "ACT  $*"; }

# ── 1. Ollama VRAM flush ──────────────────────────────────────────────────────
log "=== watchdog run ==="

OLLAMA_MODELS=$(curl -sf --max-time 5 "${OLLAMA_URL}/api/ps" 2>/dev/null || echo '{"models":[]}')
NOW_EPOCH=$(date +%s)

echo "$OLLAMA_MODELS" | python3 - <<'PYEOF'
import sys, json, os, subprocess, datetime

data = json.loads(sys.stdin.read())
models = data.get("models", [])
now = int(os.environ.get("NOW_EPOCH", "0"))
stale_sec = int(os.environ.get("STALE_THRESHOLD_SEC", "420"))
ollama_url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
log_prefix = os.environ.get("WATCHDOG_LOG_PREFIX", "")

if not models:
    print(f"{log_prefix}[ollama] no models loaded — nothing to flush")
    sys.exit(0)

for m in models:
    name = m.get("name", "?")
    exp_str = m.get("expires_at", "")
    try:
        # Parse ISO8601 with timezone offset (Python 3.11+ handles %z easily)
        exp_dt = datetime.datetime.fromisoformat(exp_str)
        exp_epoch = int(exp_dt.timestamp())
        age_sec = now - exp_epoch
    except Exception:
        age_sec = -1

    if age_sec > stale_sec:
        print(f"{log_prefix}[ollama] STALE model {name} expired {age_sec}s ago — flushing VRAM")
        # Send keep_alive:0 to unload immediately
        import urllib.request
        payload = json.dumps({"model": name, "keep_alive": 0}).encode()
        req = urllib.request.Request(
            f"{ollama_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            print(f"{log_prefix}[ollama] flushed {name}")
        except Exception as e:
            print(f"{log_prefix}[ollama] flush failed for {name}: {e}")
    else:
        secs_until = exp_epoch - now if age_sec < 0 else -age_sec
        print(f"{log_prefix}[ollama] {name} — expires in {secs_until}s (healthy)")
PYEOF

# ── 2. LiteLLM stale-request check ───────────────────────────────────────────
# LiteLLM doesn't expose a "list active requests" REST endpoint.
# We proxy the SQLite cost DB to find rows with no end_time older than
# STALE_THRESHOLD_SEC; if any exist and LiteLLM is running, restart it.

COST_DB="${REPO_ROOT}/cost/cost.db"
STALE_COUNT=0

if [[ -f "$COST_DB" ]]; then
    STALE_COUNT=$(python3 - <<PYEOF2
import sqlite3, time, os, sys
db = "$COST_DB"
threshold = $STALE_THRESHOLD_SEC
try:
    con = sqlite3.connect(db, timeout=5)
    # LiteLLM stores requests in spendlogs; rows with no endTime are open.
    rows = con.execute("""
        SELECT COUNT(*) FROM spendlogs
        WHERE endTime IS NULL
          AND startTime IS NOT NULL
          AND (strftime('%s','now') - strftime('%s', startTime)) > ?
    """, (threshold,)).fetchone()
    print(rows[0] if rows else 0)
except Exception as e:
    print(0)
PYEOF2
)
fi

if [[ "$STALE_COUNT" -gt 0 ]]; then
    warn "[litellm] $STALE_COUNT stale open request(s) older than ${STALE_THRESHOLD_SEC}s found in cost DB"
    if [[ "$STATUS_ONLY" == "false" ]]; then
        act "[litellm] restarting via launchctl to close orphaned upstream connections"
        launchctl stop "$LITELLM_PLIST"  2>/dev/null || true
        sleep 2
        launchctl start "$LITELLM_PLIST" 2>/dev/null || true
        act "[litellm] restart issued"
    else
        warn "[litellm] --status mode: restart skipped"
    fi
else
    ok "[litellm] no stale open requests in cost DB"
fi

log "=== watchdog done ==="
