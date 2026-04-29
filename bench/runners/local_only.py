"""Arm A: local-only.

Sends the task prompt to LiteLLM model `local-long` (Ollama + Qwen3-Coder-Next
with TurboQuant). Records timing, tokens, and grading into `bench_runs`.

Usage:
  python -m bench.runners.local_only --task lru_ttl_cache [--attempts 3]
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from bench import grader  # noqa: E402
from bench.runners import litellm_arm  # noqa: E402

ARM = "local-only"
DEFAULT_MODEL = "local-long"
RESULTS = REPO_ROOT / "bench" / "results"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--task", required=True, help="task id (file in bench/tasks/<id>.json)")
    p.add_argument("--model", default=DEFAULT_MODEL, help="LiteLLM model name")
    p.add_argument("--attempts", type=int, default=1)
    p.add_argument("--pytest-timeout", type=float, default=120.0)
    args = p.parse_args(argv)

    task = grader.load_task(args.task)
    print(f"[{ARM}] task={args.task} model={args.model} attempts={args.attempts}")
    for i in range(1, args.attempts + 1):
        work = RESULTS / f"{ARM}-{args.task}-{int(time.time())}-att{i}"
        row = litellm_arm.run_arm(
            arm=ARM, model=args.model, task=task, work_dir=work,
            attempt=i, pytest_timeout=args.pytest_timeout,
        )
        print(f"  att{i}: id={row['id']} "
              f"score={row['composite_score']:.3f} "
              f"pytest={row['pytest_passed']}/{row['pytest_total']} "
              f"in={row['input_tok']} out={row['output_tok']} "
              f"actual=${row['actual_cost']:.4f} shadow=${row['shadow_cost']:.4f} "
              f"wall={row['wall_ms']}ms gen={row['generate_ms']}ms "
              f"ttft={row['ttft_ms']}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
