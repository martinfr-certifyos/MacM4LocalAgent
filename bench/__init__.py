"""Benchmark harness for the local vs. claude vs. cursor-no-proxy comparison.

Layout:
  bench/
    tasks/           # task spec JSON + grader pytest files
    runners/         # arm-specific drivers (local_only.py, claude_only.py,
                     # cursor_session.py)
    results/         # per-run artifacts (generated modules, pytest output)
    schema.sql       # bench_runs + bench_summary
    db.py            # connect/insert/query helpers
    grader.py        # extract code, run pytest, score
    runner.py        # CLI entry: `python -m bench.runner ...`
    report.py        # CLI report comparing arms

Everything lives in `cost/cost.db` alongside the existing requests/comparisons
tables so the dashboard and the bench reuse the same SQLite file.
"""
