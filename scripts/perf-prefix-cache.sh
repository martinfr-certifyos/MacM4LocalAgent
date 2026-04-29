#!/usr/bin/env bash
# perf-prefix-cache.sh - Measure how much Ollama's prompt-prefix KV cache
# actually saves on a Cursor-style session.
#
# Pattern simulated:
#   Turn 1: large initial prompt   (cold prefill, defines the prefix)
#   Turn 2..N: same prefix + tiny different suffix (should hit the cache)
#
# This is the workload that dominates Cursor traffic - the agent re-reads
# the same files across many turns. If the cache is working, turn 2+
# should pay prefill only on the suffix delta.
#
# Usage:
#   bash scripts/perf-prefix-cache.sh                  # default: 80k prefix, 4 follow-ups
#   PREFIX_TOKENS=110000 FOLLOWUPS=6 bash scripts/perf-prefix-cache.sh
#   bash scripts/perf-prefix-cache.sh --evict          # bounce ollama before turn 1 (true cold)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO_ROOT/config/detected.env"

PREFIX_TOKENS="${PREFIX_TOKENS:-80000}"
FOLLOWUPS="${FOLLOWUPS:-4}"
NUM_PREDICT="${NUM_PREDICT:-32}"
EVICT=0
[[ "${1:-}" == "--evict" ]] && EVICT=1

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
hdr()  { printf "\n\033[1;36m== %s ==\033[0m\n" "$*"; }
ok()   { printf "  \033[1;32m%s\033[0m\n" "$*"; }
warn() { printf "  \033[1;33m%s\033[0m\n" "$*"; }

# ----------------------------------------------------------------------
hdr "Pre-flight"
echo "  prefix target:   ${PREFIX_TOKENS} router tokens"
echo "  follow-ups:      ${FOLLOWUPS}"
echo "  num_predict:     ${NUM_PREDICT}"
echo "  ollama tag:      ${OLLAMA_TAG}"

# Read actual ollama process env, not the session-global launchctl vars
# (the launchd job sets these scoped to the process, not session-wide).
PID_OLL=$(lsof -nP -iTCP:11434 -sTCP:LISTEN -t 2>/dev/null | head -1 || true)
if [[ -n "$PID_OLL" ]]; then
  proc_env=$(ps -E -p "$PID_OLL" 2>/dev/null | tr ' ' '\n' | grep -E '^OLLAMA_' || true)
  np=$(echo "$proc_env" | awk -F= '/^OLLAMA_NUM_PARALLEL=/ {print $2}')
  ka=$(echo "$proc_env" | awk -F= '/^OLLAMA_KEEP_ALIVE=/ {print $2}')
  kv=$(echo "$proc_env" | awk -F= '/^OLLAMA_KV_CACHE_TYPE=/ {print $2}')
  echo "  num_parallel:    ${np:-<unset>}    (ollama pid $PID_OLL)"
  echo "  keep_alive:      ${ka:-<unset>}"
  echo "  kv_cache_type:   ${kv:-<unset>}"
else
  echo "  ollama:          <not running>"
fi

if (( EVICT == 1 )); then
  warn "--evict: bouncing Ollama to flush any cached prefix before turn 1"
  PID=$(lsof -nP -iTCP:11434 -sTCP:LISTEN -t 2>/dev/null | head -1)
  [[ -n "$PID" ]] && kill -TERM "$PID" || true
  for i in 1 2 3 4 5 6 7 8; do
    sleep 1
    NEW=$(lsof -nP -iTCP:11434 -sTCP:LISTEN -t 2>/dev/null | head -1)
    if [[ -n "$NEW" && "$NEW" != "$PID" ]]; then
      ok "respawned (pid=$NEW) after ${i}s"
      break
    fi
  done
fi

# Drive the whole experiment from one python process. Bash + jq + heredoc
# math is too fragile when prompts get this big.
python3 - "$PREFIX_TOKENS" "$FOLLOWUPS" "$NUM_PREDICT" "$OLLAMA_TAG" <<'PY'
import json, sys, time, urllib.request, urllib.error

prefix_tokens = int(sys.argv[1])
followups     = int(sys.argv[2])
num_predict   = int(sys.argv[3])
model         = sys.argv[4]
url           = "http://127.0.0.1:11434/api/chat"

# Build the shared prefix.
chunk = "// noise line, kept short and uniform so token estimate is stable.\n"
target_chars = prefix_tokens * 36 // 10
prefix = chunk * max(1, target_chars // len(chunk))

# Distinct suffixes for each turn so the model has to do *some* fresh
# work per turn (otherwise Ollama might short-circuit the entire request).
suffixes = [
    "\nIn one sentence, what is the dominant repeated string above?",
    "\nIn one sentence, how many words are in the repeated phrase above?",
    "\nIn one sentence, does the noise above look like Python or C?",
    "\nIn one sentence, what punctuation appears most often above?",
    "\nIn one sentence, what is the most common word above?",
    "\nIn one sentence, what is the file extension hinted above?",
    "\nIn one sentence, are the lines above uniform in length?",
    "\nIn one sentence, identify the comment style used above.",
]
turns = [{"label": "turn 1 (cold prefix establishment)",   "suffix": suffixes[0]}]
for i in range(followups):
    s = suffixes[(i + 1) % len(suffixes)]
    turns.append({"label": f"turn {i+2} (same prefix, suffix #{i+1})", "suffix": s})

def call(content):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "options": {"num_predict": num_predict},
    }).encode()
    t0 = time.time()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1500) as r:
        d = json.loads(r.read())
    return time.time() - t0, d

print()
print("\033[1;36m== Running turns ==\033[0m")
print(f"  {'turn':<48} {'wall':>8}  {'prefill_tok':>12} {'prefill_s':>10} {'tok/s in':>10}  {'decode_s':>9} {'tok/s out':>10}")
print("  " + "-" * 118)

results = []
for t in turns:
    content = prefix + t["suffix"]
    wall, d = call(content)
    pt = d.get("prompt_eval_count", 0) or 0
    pd = (d.get("prompt_eval_duration", 0) or 0) / 1e9
    et = d.get("eval_count", 0) or 0
    ed = (d.get("eval_duration", 0) or 0) / 1e9
    rate_in  = (pt / pd) if pd > 0 else 0.0
    rate_out = (et / ed) if ed > 0 else 0.0
    results.append({"label": t["label"], "wall": wall, "pt": pt, "pd": pd, "et": et, "ed": ed})
    print(f"  {t['label']:<48} {wall:>7.2f}s  {pt:>12,d} {pd:>9.2f}s {rate_in:>9.0f}  {ed:>8.2f}s {rate_out:>9.1f}")

# ----- summary -----
print()
print("\033[1;36m== Cache analysis ==\033[0m")
turn1 = results[0]
followup_results = results[1:]
mean_followup_prefill = sum(r["pd"] for r in followup_results) / max(1, len(followup_results))
saved_per_turn = turn1["pd"] - mean_followup_prefill
total_saved    = saved_per_turn * len(followup_results)
naive_total    = turn1["pd"] * len(results)
actual_total   = sum(r["pd"] for r in results)
speedup_total  = (naive_total / actual_total) if actual_total > 0 else 0
hit_ratio_pct  = (1.0 - mean_followup_prefill / turn1["pd"]) * 100 if turn1["pd"] > 0 else 0.0

print(f"  turn 1 prefill                          : {turn1['pd']:>7.2f} s  ({turn1['pt']:>6,d} tok)")
print(f"  mean follow-up prefill ({len(followup_results)} turns)        : {mean_followup_prefill:>7.2f} s")
print(f"  prefill time saved per follow-up        : {saved_per_turn:>7.2f} s")
print(f"  prefill time saved across all follow-ups: {total_saved:>7.2f} s")
print(f"  cache hit ratio (1 - followup/turn1)    : {hit_ratio_pct:>7.1f} %")
print(f"  effective prefill speedup vs naive      : {speedup_total:>7.1f}x")

if hit_ratio_pct > 80:
    print("\n  \033[1;32mPASS\033[0m  prefix cache is healthy (>80% hit ratio).")
elif hit_ratio_pct > 30:
    print("\n  \033[1;33mPARTIAL\033[0m  prefix cache is partially effective.")
else:
    print("\n  \033[1;31mFAIL\033[0m  prefix cache appears not to be re-used. Possible causes:")
    print("        - OLLAMA_KEEP_ALIVE too short and model is being evicted between turns")
    print("        - Some upstream layer is mutating the prompt prefix (e.g. role/system message rewriting)")
    print("        - Ollama version does not support the cache for this model family")
PY
