"""Arm C: cursor-no-proxy (and arm C': cursor-hybrid).

Cursor with the local LiteLLM override DISABLED (or with a different model
selected, in the hybrid case) doesn't route through our proxy, so we cannot
intercept token counts or latency with our own callbacks. To still measure
this arm fairly we ingest a recorded session from a JSON file, then anchor
the window against Cursor's billing surface (admin API or manual CSV).

Workflow for the operator:
  1. Open Cursor, ensure `Override OpenAI Base URL` is OFF (arm cursor-no-proxy)
     or ON pointing to LiteLLM with `hybrid-auto` (arm cursor-hybrid).
  2. Paste the task prompt into Cursor (Ask mode is fine).
  3. Note the start time, hit submit, wait for full completion, note end time.
  4. Save the model's response to `bench/results/<arm>-<task>-<ts>/output.txt`.
  5. Fill `bench/results/<arm>-<task>-<ts>/session.json`:
        {
          "arm": "cursor-no-proxy",
          "task_id": "lru_ttl_cache",
          "model": "claude-sonnet-4-6",   // as Cursor reports it
          "start_ts": 1714000000,
          "end_ts":   1714000087,
          "ttft_ms":  1300,
          "input_tokens":  4321,           // optional; from Cursor request panel
          "output_tokens": 1840,           // optional
          "notes": "ran in Ask mode, no rules applied"
        }
  6. Run:
        python -m bench.runners.cursor_session \
            --session bench/results/<arm>-<task>-<ts>/session.json \
            --output  bench/results/<arm>-<task>-<ts>/output.txt
     The harness writes a `bench_runs` row + grades the output exactly the same
     way as the automated arms.

After all attempts, pull provider spend:
        python -m bench.pull_spend --arm cursor-no-proxy --task-id <id> \
            --window-start <earliest start_ts> --window-end <latest end_ts> \
            --providers cursor
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from bench import db, grader  # noqa: E402

VALID_ARMS = ("cursor-no-proxy", "cursor-hybrid")
RESULTS = REPO_ROOT / "bench" / "results"


def ingest(
    *,
    session_path: pathlib.Path,
    output_path: pathlib.Path,
    pytest_timeout: float = 120.0,
) -> dict:
    session = json.loads(session_path.read_text())
    arm = session.get("arm", "cursor-no-proxy")
    if arm not in VALID_ARMS:
        raise ValueError(f"arm must be one of {VALID_ARMS}, got {arm!r}")
    task_id = session["task_id"]
    task = grader.load_task(task_id)

    output_text = output_path.read_text() if output_path.exists() else ""
    work_dir = session_path.parent / "grade"
    gr = grader.grade_task(task, output_text, work_dir=work_dir)

    start_ts = int(session.get("start_ts") or int(time.time()))
    end_ts   = int(session.get("end_ts")   or start_ts)
    wall_ms  = max(0, (end_ts - start_ts) * 1000)
    ttft_ms  = int(session.get("ttft_ms") or 0)

    in_tok  = int(session.get("input_tokens", 0) or 0)
    out_tok = int(session.get("output_tokens", 0) or 0)

    # We don't know Cursor's billing for THIS specific call until pull_spend
    # runs; record actual=0 here and let the reporter overlay provider spend.
    row = {
        "ts": start_ts,
        "task_id": task_id,
        "arm": arm,
        "model": session.get("model", "cursor-unknown"),
        "attempt": int(session.get("attempt", 1) or 1),
        "input_tok":  in_tok,
        "output_tok": out_tok,
        "actual_cost": 0.0,
        "shadow_cost": (in_tok * (3.0 / 1_000_000)) + (out_tok * (15.0 / 1_000_000)),
        "wall_ms":     wall_ms,
        "generate_ms": wall_ms,
        "ttft_ms":     ttft_ms,
        "grade_ms":    gr.grade_ms,
        "output_chars": len(output_text),
        "notes": session.get("notes", "")[:500],
        "raw_metadata": {
            "session_path": str(session_path),
            "output_path":  str(output_path),
            "session":      session,
        },
        **gr.as_db_row(),
    }
    rid = db.record_run(row)
    row["id"] = rid
    return row


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--session", required=True, help="path to session.json")
    p.add_argument("--output",  required=True, help="path to model output.txt")
    p.add_argument("--pytest-timeout", type=float, default=120.0)
    args = p.parse_args(argv)
    row = ingest(
        session_path=pathlib.Path(args.session),
        output_path=pathlib.Path(args.output),
        pytest_timeout=args.pytest_timeout,
    )
    print(f"[{row['arm']}] id={row['id']} task={row['task_id']} "
          f"score={row['composite_score']:.3f} "
          f"pytest={row['pytest_passed']}/{row['pytest_total']} "
          f"in={row['input_tok']} out={row['output_tok']} "
          f"wall={row['wall_ms']}ms ttft={row['ttft_ms']}ms")
    print(f"\nNext step (anchor to Cursor billing):\n"
          f"  python -m bench.pull_spend --arm {row['arm']} "
          f"--task-id {row['task_id']} "
          f"--window-start {row['ts']} --window-end {row['ts'] + max(1, row['wall_ms']//1000)} "
          f"--providers cursor")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
