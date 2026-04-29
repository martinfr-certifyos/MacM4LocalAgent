#!/usr/bin/env bash
# Convenience entrypoint - runs `make install`.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
exec make install "$@"
