"""Arm B: claude-only.

Sends the task prompt to LiteLLM model `claude-code` (Anthropic Sonnet 4.6 via
the proxy, so we get the same instrumentation as local-only). After the run,
the operator should invoke `python -m bench.pull_spend --arm claude-only ...`
to anchor the run window against the Anthropic Admin Usage API.

Usage:
  python -m bench.runners.claude_only --task lru_ttl_cache [--attempts 3]
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

ARM = "claude-only"
DEFAULT_MODEL = "claude-code"
RESULTS = REPO_ROOT / "bench" / "results"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--task", required=True)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--attempts", type=int, default=1)
    p.add_argument("--pytest-timeout", type=float, default=120.0)
    args = p.parse_args(argv)

    task = grader.load_task(args.task)
    started = int(time.time())
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
    ended = int(time.time())
    print(f"\nRun window: {started} .. {ended}")
    print(f"Anchor provider spend with:\n"
          f"  python -m bench.pull_spend --arm {ARM} --task-id {args.task} "
          f"--window-start {started} --window-end {ended} --providers anthropic")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
