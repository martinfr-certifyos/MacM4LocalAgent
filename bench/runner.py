"""Top-level CLI: run the two automated arms (local-only, claude-only) on a
single task and emit the time windows the operator needs to (a) record the
Cursor session, and (b) pull provider-billed spend afterwards.

Examples:
  python -m bench.runner --task lru_ttl_cache
  python -m bench.runner --task lru_ttl_cache --attempts 3
  python -m bench.runner --task lru_ttl_cache --arms local-only,claude-only
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from bench import grader  # noqa: E402
from bench.runners import litellm_arm  # noqa: E402

ARM_TO_MODEL = {
    "local-only":  "local-long",
    "claude-only": "claude-code",
}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--task", required=True)
    p.add_argument("--arms", default="local-only,claude-only",
                   help="comma-separated subset of {local-only,claude-only}")
    p.add_argument("--attempts", type=int, default=1)
    p.add_argument("--pytest-timeout", type=float, default=120.0)
    args = p.parse_args(argv)

    task = grader.load_task(args.task)
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    for a in arms:
        if a not in ARM_TO_MODEL:
            print(f"unknown arm: {a}", file=sys.stderr)
            return 2

    windows: dict[str, tuple[int, int]] = {}
    for arm in arms:
        model = ARM_TO_MODEL[arm]
        print(f"\n=== {arm} ({model}) ===")
        started = int(time.time())
        for i in range(1, args.attempts + 1):
            work = REPO_ROOT / "bench" / "results" / f"{arm}-{args.task}-{int(time.time())}-att{i}"
            row = litellm_arm.run_arm(
                arm=arm, model=model, task=task, work_dir=work,
                attempt=i, pytest_timeout=args.pytest_timeout,
            )
            print(f"  att{i}: id={row['id']} score={row['composite_score']:.3f} "
                  f"pytest={row['pytest_passed']}/{row['pytest_total']} "
                  f"in={row['input_tok']} out={row['output_tok']} "
                  f"actual=${row['actual_cost']:.4f} "
                  f"wall={row['wall_ms']}ms gen={row['generate_ms']}ms "
                  f"ttft={row['ttft_ms']}ms")
        ended = int(time.time())
        windows[arm] = (started, ended)

    # Print follow-up spend-pull commands so the user can attribute provider
    # billing to each arm window.
    print("\n=== Provider-spend follow-up ===")
    for arm, (s, e) in windows.items():
        if arm == "claude-only":
            print(f"  python -m bench.pull_spend --arm {arm} --task-id {args.task} "
                  f"--window-start {s} --window-end {e} --providers anthropic")
    print("\nFor the Cursor arms, after recording sessions:")
    print(f"  python -m bench.pull_spend --arm cursor-no-proxy --task-id {args.task} "
          f"--window-start <earliest> --window-end <latest> --providers cursor")

    print("\nReport:\n  python -m bench.report --task " + args.task)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
