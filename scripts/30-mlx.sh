#!/usr/bin/env bash
# 30-mlx.sh - Install mlx-lm and download an MLX-quantized Qwen3-Coder-Next.
# MLX is the fast tier (<16k context). No TurboQuant in MLX yet.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO_ROOT/config/detected.env"

log() { printf "\033[1;34m[mlx]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[mlx]\033[0m %s\n" "$*" >&2; }

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found; run scripts/10-brew.sh first" >&2
  exit 1
fi

VENV="$REPO_ROOT/.venvs/mlx"
if [[ ! -d "$VENV" ]]; then
  log "creating venv at $VENV"
  uv venv --python 3.12 "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

log "installing mlx-lm + huggingface_hub"
uv pip install --upgrade pip >/dev/null
# hf_transfer = parallel-chunked download, fixes the "stalls forever on big repos" problem.
uv pip install --upgrade "mlx-lm>=0.20" "huggingface_hub>=0.24" "hf_transfer>=0.1" "fastapi>=0.110" "uvicorn>=0.30"
export HF_HUB_ENABLE_HF_TRANSFER=1

# Pick an MLX-converted model repo. Try the most likely community tags in order.
# We always include a known-small fallback (~4-6 GB) so a fresh install can complete
# even on flaky residential networks.
#
# NOTE: As of 2026-04 the Qwen3-Coder-Next MLX repos are stored on HF Xet-CAS
# and frequently return HTTP 416 errors for the larger shards on unauthenticated
# downloads. We list them first (best quality) but fall back to plain-LFS
# Qwen2.5-Coder repos that work reliably without an HF_TOKEN. Set
# MLX_PREFER_NEXT=0 to skip the unstable repos entirely.
case "$MLX_QUANT" in
  8bit) MLX_REPO_CANDIDATES=(
          "mlx-community/Qwen3-Coder-Next-8bit"
          "mlx-community/Qwen3-Coder-30B-A3B-Instruct-8bit"
          "mlx-community/Qwen2.5-Coder-32B-Instruct-8bit"
          "mlx-community/Qwen2.5-Coder-7B-Instruct-8bit"
          "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"
        ) ;;
  4bit) MLX_REPO_CANDIDATES=(
          "mlx-community/Qwen3-Coder-Next-4bit"
          "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit"
          "mlx-community/Qwen2.5-Coder-32B-Instruct-4bit"
          "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"
        ) ;;
  *)    MLX_REPO_CANDIDATES=("mlx-community/Qwen2.5-Coder-7B-Instruct-4bit") ;;
esac

# Allow user to skip the unstable Qwen3-Coder-Next repos entirely.
if [[ "${MLX_PREFER_NEXT:-1}" == "0" ]]; then
  filtered=()
  for repo in "${MLX_REPO_CANDIDATES[@]}"; do
    case "$repo" in
      *Qwen3-Coder-Next*) ;;
      *) filtered+=("$repo") ;;
    esac
  done
  MLX_REPO_CANDIDATES=("${filtered[@]}")
fi

MODELS_DIR="$REPO_ROOT/models"
mkdir -p "$MODELS_DIR"

# Walk through candidates, downloading the first one that completes.
# DOWNLOAD_TIMEOUT (seconds) protects against repos that wedge in retry loops
# (e.g. HF Xet 416 errors). Override with DOWNLOAD_TIMEOUT=N for slow links.
DOWNLOAD_TIMEOUT="${DOWNLOAD_TIMEOUT:-1800}"
STALL_WINDOW="${STALL_WINDOW:-180}"

download_repo() {
  local repo="$1"
  local target="$2"
  log "downloading $repo -> $target (timeout ${DOWNLOAD_TIMEOUT}s)"
  python -c "
import os, sys, threading, time, pathlib
from huggingface_hub import snapshot_download

target = '$target'
timeout = $DOWNLOAD_TIMEOUT
stall_window = $STALL_WINDOW
done = threading.Event()
result = {}

def watchdog():
    last_size = -1
    last_change = time.time()
    deadline = last_change + timeout
    while not done.is_set():
        time.sleep(15)
        size = 0
        p = pathlib.Path(target)
        if p.exists():
            for f in p.rglob('*'):
                try:
                    if f.is_file():
                        size += f.stat().st_size
                except OSError:
                    pass
        if size != last_size:
            last_size = size
            last_change = time.time()
        if time.time() - last_change > stall_window:
            print(f'STALL: no progress for {stall_window}s; aborting', file=sys.stderr)
            os._exit(2)
        if time.time() > deadline:
            print(f'TIMEOUT: download exceeded {timeout}s', file=sys.stderr)
            os._exit(3)

t = threading.Thread(target=watchdog, daemon=True)
t.start()

try:
    snapshot_download(
        repo_id='$repo',
        local_dir=target,
        max_workers=8,
    )
    print('OK')
except Exception as e:
    print(f'FAIL: {e}', file=sys.stderr)
    sys.exit(1)
finally:
    done.set()
"
}

LOCAL_DIR=""
SUCCESS_REPO=""
for repo in "${MLX_REPO_CANDIDATES[@]}"; do
  candidate_dir="$MODELS_DIR/$(echo "$repo" | tr '/' '_')"
  # Treat "no safetensors yet" as "incomplete; retry from scratch".
  if [[ -d "$candidate_dir" ]] && ls "$candidate_dir"/*.safetensors >/dev/null 2>&1; then
    log "already downloaded: $repo at $candidate_dir"
    LOCAL_DIR="$candidate_dir"
    SUCCESS_REPO="$repo"
    break
  fi
  rm -rf "$candidate_dir"
  if download_repo "$repo" "$candidate_dir"; then
    LOCAL_DIR="$candidate_dir"
    SUCCESS_REPO="$repo"
    break
  fi
  warn "download failed for $repo; trying next candidate"
  rm -rf "$candidate_dir"
done

if [[ -z "$LOCAL_DIR" ]]; then
  warn "All MLX candidate downloads failed."
  warn "You can re-run this script later, or set HF_TOKEN to avoid HF rate limits."
  PICKED="${MLX_REPO_CANDIDATES[-1]}"
  LOCAL_DIR="$MODELS_DIR/$(echo "$PICKED" | tr '/' '_')"
else
  PICKED="$SUCCESS_REPO"
  log "MLX model ready: $PICKED"
fi

# Persist the chosen MLX repo + path back to detected.env (append if not already there).
if ! grep -q "^MLX_REPO=" "$REPO_ROOT/config/detected.env"; then
  {
    echo ""
    echo "MLX_REPO=\"$PICKED\""
    echo "MLX_LOCAL_DIR=\"$LOCAL_DIR\""
  } >> "$REPO_ROOT/config/detected.env"
else
  sed -i.bak "s|^MLX_REPO=.*|MLX_REPO=\"$PICKED\"|"        "$REPO_ROOT/config/detected.env"
  sed -i.bak "s|^MLX_LOCAL_DIR=.*|MLX_LOCAL_DIR=\"$LOCAL_DIR\"|" "$REPO_ROOT/config/detected.env"
fi

log "done"
