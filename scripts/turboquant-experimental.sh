#!/usr/bin/env bash
# turboquant-experimental.sh - OPT-IN: build a llama.cpp fork with the
# TurboQuant KV-cache PR (#21131) cherry-picked, and run it on an
# alternate port (default 8082) for A/B testing against the live Ollama
# backend on :11434.
#
# Why isolated?
#   - PR is unmerged, has churning APIs, and was last rebased Apr 2026.
#   - We do NOT want this to become a hard dependency of the main stack.
#   - Run it side-by-side; if it falls behind upstream you simply stop it.
#
# Usage:
#   bash scripts/turboquant-experimental.sh build       # clone + cherry-pick + cmake build
#   bash scripts/turboquant-experimental.sh serve       # start llama-server on :8082
#   bash scripts/turboquant-experimental.sh stop        # kill the server
#   bash scripts/turboquant-experimental.sh status      # is it up?
#   bash scripts/turboquant-experimental.sh ab "prompt" # send same prompt to :11434 vs :8082, diff timings
#   bash scripts/turboquant-experimental.sh nuke        # remove worktree + binary
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO_ROOT/config/detected.env"

WORK_DIR="$REPO_ROOT/.experimental/llama-tq"
SRC_DIR="$WORK_DIR/llama.cpp"
BUILD_DIR="$SRC_DIR/build"
BIN="$BUILD_DIR/bin/llama-server"
PIDFILE="$WORK_DIR/llama-server.pid"
LOGFILE="$REPO_ROOT/.logs/llama-tq.log"
PORT="${TQ_EXP_PORT:-8082}"
PR_NUM="${TQ_EXP_PR:-21131}"
GGUF_PATH="${TQ_EXP_GGUF:-$HOME/.cache/huggingface/hub/models--unsloth--Qwen3-Coder-Next-GGUF/snapshots}"
KV_FLAG="${TQ_EXP_KV:-tq3}"  # tq3 or tq4

mkdir -p "$WORK_DIR" "$REPO_ROOT/.logs"

log()  { printf "\033[1;34m[tq-exp]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[tq-exp]\033[0m %s\n" "$*" >&2; }
ok()   { printf "\033[1;32m[tq-exp]\033[0m %s\n" "$*"; }
die()  { printf "\033[1;31m[tq-exp]\033[0m %s\n" "$*" >&2; exit 1; }

require_brew() {
  command -v "$1" >/dev/null 2>&1 || die "missing '$1' - run: brew install $1"
}

cmd_build() {
  require_brew git
  require_brew cmake
  require_brew ninja
  log "fetching llama.cpp + PR #$PR_NUM into $SRC_DIR"
  if [[ ! -d "$SRC_DIR/.git" ]]; then
    git clone --depth 50 https://github.com/ggml-org/llama.cpp.git "$SRC_DIR"
  fi
  cd "$SRC_DIR"
  git fetch origin master --depth 50
  git fetch origin "pull/$PR_NUM/head:tq-pr" --depth 50 || die "could not fetch PR #$PR_NUM. Has it been closed?"
  git reset --hard origin/master
  log "cherry-picking PR #$PR_NUM"
  if ! git merge --no-edit tq-pr; then
    warn "merge had conflicts. Drop into $SRC_DIR and resolve, then re-run."
    exit 1
  fi
  log "configuring (Metal on)"
  cmake -B "$BUILD_DIR" -G Ninja \
    -DGGML_METAL=ON \
    -DLLAMA_CURL=OFF \
    -DCMAKE_BUILD_TYPE=Release
  log "building llama-server"
  cmake --build "$BUILD_DIR" --config Release --target llama-server -j
  ok "built: $BIN"
}

find_gguf() {
  # Reuse the GGUF we already downloaded for Ollama if present.
  local p
  p="$(ls -1 "$GGUF_PATH"/*/Qwen3-Coder-Next*.gguf 2>/dev/null | head -1 || true)"
  if [[ -z "$p" ]]; then
    # Common alternate location used by the install
    p="$(ls -1 "$REPO_ROOT/models/ollama-hf"/*.gguf 2>/dev/null | head -1 || true)"
  fi
  echo "$p"
}

cmd_serve() {
  [[ -x "$BIN" ]] || die "binary missing. Run: bash $0 build"
  local gguf
  gguf="$(find_gguf)"
  [[ -n "$gguf" ]] || die "no GGUF found. Set TQ_EXP_GGUF=/abs/path/to/model.gguf"
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    warn "already running (pid $(cat "$PIDFILE"))"
    return 0
  fi
  log "starting llama-server on :$PORT with --turbo-kv $KV_FLAG"
  log "  gguf:  $gguf"
  log "  log:   $LOGFILE"
  nohup "$BIN" \
    --host 127.0.0.1 \
    --port "$PORT" \
    -m "$gguf" \
    --ctx-size 65536 \
    --flash-attn \
    --turbo-kv "$KV_FLAG" \
    --n-gpu-layers 999 \
    >>"$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  sleep 2
  if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    ok "running (pid $(cat "$PIDFILE")). Endpoint: http://127.0.0.1:$PORT/v1"
  else
    rm -f "$PIDFILE"
    die "failed to start; tail -50 $LOGFILE"
  fi
}

cmd_stop() {
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill "$(cat "$PIDFILE")" || true
    rm -f "$PIDFILE"
    ok "stopped."
  else
    log "not running."
  fi
}

cmd_status() {
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    ok "running (pid $(cat "$PIDFILE")) on :$PORT"
    curl -fsS "http://127.0.0.1:$PORT/v1/models" | head -c 300; echo
  else
    log "not running."
  fi
}

cmd_ab() {
  local prompt="${1:-Write a one-line Python function that returns x+1.}"
  command -v jq >/dev/null || die "need jq (brew install jq)"
  log "A/B: same prompt against live Ollama (:$OLLAMA_PORT) and experimental llama.cpp (:$PORT)"
  body="$(jq -n --arg p "$prompt" \
    '{model:"qwen3-coder-next:q4_K_M", messages:[{role:"user", content:$p}], max_tokens:128}')"

  echo
  log "[A] live Ollama (KV=$KV_CACHE_TYPE)"
  t0=$(python3 -c "import time;print(time.time())")
  curl -fsS -m 120 -H "Content-Type: application/json" \
    -d "$body" "http://127.0.0.1:$OLLAMA_PORT/v1/chat/completions" \
    | tee "$WORK_DIR/ab-live.json" | jq -r '.choices[0].message.content' || true
  t1=$(python3 -c "import time;print(time.time())")
  printf "    elapsed: %.2fs\n" "$(python3 -c "print($t1-$t0)")"

  echo
  log "[B] experimental llama.cpp (--turbo-kv $KV_FLAG)"
  t0=$(python3 -c "import time;print(time.time())")
  curl -fsS -m 120 -H "Content-Type: application/json" \
    -d "$body" "http://127.0.0.1:$PORT/v1/chat/completions" \
    | tee "$WORK_DIR/ab-exp.json" | jq -r '.choices[0].message.content' || true
  t1=$(python3 -c "import time;print(time.time())")
  printf "    elapsed: %.2fs\n" "$(python3 -c "print($t1-$t0)")"

  echo
  ok "outputs saved to $WORK_DIR/ab-{live,exp}.json"
}

cmd_nuke() {
  cmd_stop || true
  rm -rf "$WORK_DIR"
  ok "removed $WORK_DIR"
}

case "${1:-status}" in
  build)  cmd_build  ;;
  serve)  cmd_serve  ;;
  stop)   cmd_stop   ;;
  status) cmd_status ;;
  ab)     shift; cmd_ab "$@" ;;
  nuke)   cmd_nuke   ;;
  *) cat <<EOF
Usage: $0 {build|serve|stop|status|ab "prompt"|nuke}
Env overrides:
  TQ_EXP_PORT  (default 8082)
  TQ_EXP_PR    (default 21131)
  TQ_EXP_KV    (tq3 or tq4, default tq3)
  TQ_EXP_GGUF  (path to a Qwen3-Coder-Next GGUF; auto-discovered)
EOF
    exit 1 ;;
esac
