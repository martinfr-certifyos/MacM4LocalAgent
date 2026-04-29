"""Tiny launcher that imports and runs the LiteLLM proxy.

Equivalent to the `litellm` console-script wrapper but invoked directly via
`python` so launchd / TCC don't choke on the venv wrapper's pyvenv.cfg
provenance check on macOS.

Also force-inserts the repo root onto sys.path so the YAML's
`router.route_by_size.SizeBasedRouter` callback can be imported even if
PYTHONPATH gets stripped by the proxy startup hooks.
"""
from __future__ import annotations

import os
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("PYTHONPATH", str(REPO_ROOT))

# Touch the import so any early ImportError surfaces here, not deep inside
# the LiteLLM startup sequence.
import router.route_by_size  # noqa: F401

from litellm import run_server

if __name__ == "__main__":
    sys.exit(run_server())
