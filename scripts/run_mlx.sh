#!/bin/bash
# Wrapper for mlx_lm.server launched by launchd.
#
# launchd LaunchAgents can fail to access Metal GPU when the Python venv
# binary is invoked directly due to com.apple.provenance xattr restrictions
# on pyvenv.cfg. Using a plain bash wrapper bypasses that path: bash is a
# system binary with no provenance xattr, and the exec below runs Python
# in-process so it inherits bash's session context.
#
# Usage (from launchd ProgramArguments): /bin/bash /path/to/run_mlx.sh

set -euo pipefail

echo "[run_mlx.sh] starting at $(date), pid=$$, user=$(id -un)" >&2

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec "${REPO_ROOT}/.venvs/mlx/bin/python" \
    -m mlx_lm server \
    --model "${MLX_LOCAL_DIR:-${REPO_ROOT}/models/mlx-community_Qwen2.5-Coder-7B-Instruct-4bit}" \
    --host 127.0.0.1 \
    --port "${MLX_PORT:-8081}"
