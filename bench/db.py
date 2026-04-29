"""SQLite helpers for the bench harness.

We share `cost/cost.db` with the rest of the app — the bench tables are
namespaced (`bench_*`) so they coexist with `requests` and `comparisons`.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import time
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "cost" / "cost.db"
BENCH_SCHEMA = pathlib.Path(__file__).with_name("schema.sql")
COST_SCHEMA = REPO_ROOT / "cost" / "schema.sql"


def connect(db_path: pathlib.Path | None = None) -> sqlite3.Connection:
    """Open (and lazily create) the bench/cost DB."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    if COST_SCHEMA.exists():
        conn.executescript(COST_SCHEMA.read_text())
    conn.executescript(BENCH_SCHEMA.read_text())
    conn.row_factory = sqlite3.Row
    return conn


def record_run(row: dict[str, Any], *, db_path: pathlib.Path | None = None) -> int:
    """Insert one bench_runs row. `row` may omit any field; defaults applied."""
    defaults: dict[str, Any] = {
        "ts": int(time.time()),
        "attempt": 1,
        "input_tok": 0, "output_tok": 0,
        "actual_cost": 0.0, "shadow_cost": 0.0,
        "wall_ms": 0, "generate_ms": 0, "ttft_ms": 0, "grade_ms": 0,
        "output_chars": 0, "output_path": "",
        "pytest_passed": 0, "pytest_failed": 0, "pytest_errors": 0, "pytest_total": 0,
        "passes_tests": 0.0, "no_thirdparty": 0, "has_docstring": 0,
        "has_type_hints": 0, "syntactic_ok": 0,
        "composite_score": 0.0,
        "notes": "",
        "raw_metadata": "{}",
    }
    merged = {**defaults, **row}
    if not isinstance(merged["raw_metadata"], str):
        merged["raw_metadata"] = json.dumps(merged["raw_metadata"], default=str)

    cols = [
        "ts", "task_id", "arm", "model", "attempt",
        "input_tok", "output_tok", "actual_cost", "shadow_cost",
        "wall_ms", "generate_ms", "ttft_ms", "grade_ms",
        "output_chars", "output_path",
        "pytest_passed", "pytest_failed", "pytest_errors", "pytest_total",
        "passes_tests", "no_thirdparty", "has_docstring",
        "has_type_hints", "syntactic_ok",
        "composite_score", "notes", "raw_metadata",
    ]
    for c in ("task_id", "arm", "model"):
        if c not in merged:
            raise ValueError(f"record_run requires {c}")
    placeholders = ",".join("?" * len(cols))
    conn = connect(db_path)
    try:
        cur = conn.execute(
            f"INSERT INTO bench_runs ({','.join(cols)}) VALUES ({placeholders})",
            tuple(merged[c] for c in cols),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def list_runs(
    *,
    task_id: str | None = None,
    arm: str | None = None,
    db_path: pathlib.Path | None = None,
) -> list[sqlite3.Row]:
    conn = connect(db_path)
    try:
        q = "SELECT * FROM bench_runs WHERE 1=1"
        args: list[Any] = []
        if task_id is not None:
            q += " AND task_id = ?"; args.append(task_id)
        if arm is not None:
            q += " AND arm = ?"; args.append(arm)
        q += " ORDER BY ts ASC"
        return list(conn.execute(q, args).fetchall())
    finally:
        conn.close()


def record_provider_spend(
    row: dict[str, Any],
    *,
    db_path: pathlib.Path | None = None,
) -> int:
    """Insert one provider_spend row (Anthropic or Cursor snapshot)."""
    defaults: dict[str, Any] = {
        "ts": int(time.time()),
        "task_id": "",
        "input_tok": 0, "output_tok": 0,
        "cache_read_tok": 0, "cache_write_tok": 0,
        "requests": 0,
        "billed_usd": 0.0,
        "api_key_id": "",
        "user_email": "",
        "raw_response": "{}",
    }
    merged = {**defaults, **row}
    if not isinstance(merged["raw_response"], str):
        merged["raw_response"] = json.dumps(merged["raw_response"], default=str)
    for c in ("arm", "window_start", "window_end", "provider", "source"):
        if c not in merged:
            raise ValueError(f"record_provider_spend requires {c}")

    cols = [
        "ts", "arm", "task_id", "window_start", "window_end",
        "provider", "source",
        "input_tok", "output_tok", "cache_read_tok", "cache_write_tok",
        "requests", "billed_usd",
        "api_key_id", "user_email", "raw_response",
    ]
    placeholders = ",".join("?" * len(cols))
    conn = connect(db_path)
    try:
        cur = conn.execute(
            f"INSERT INTO provider_spend ({','.join(cols)}) "
            f"VALUES ({placeholders})",
            tuple(merged[c] for c in cols),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def provider_spend_for_window(
    *,
    arm: str,
    window_start: int,
    window_end: int,
    db_path: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Sum billed_usd + tokens across providers for a single arm's window.

    Matches any provider_spend row whose stored window OVERLAPS the requested
    [window_start, window_end] interval. Two intervals [a, b] and [c, d]
    overlap iff a <= d AND c <= b. This lets the reporter use a tight
    [run_ts, run_ts + wall_ms] window and still pull in spend snapshots that
    were taken with looser (e.g. day-aligned) buckets, which is exactly how
    Anthropic's daily cost endpoint returns data.
    """
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT provider, source,
                   SUM(billed_usd)      AS billed,
                   SUM(input_tok)       AS in_tok,
                   SUM(output_tok)      AS out_tok,
                   SUM(cache_read_tok)  AS cr_tok,
                   SUM(cache_write_tok) AS cw_tok,
                   SUM(requests)        AS reqs,
                   COUNT(*)             AS snaps
            FROM provider_spend
            WHERE arm = ?
              AND window_start <= ?
              AND window_end   >= ?
            GROUP BY provider, source
            """,
            (arm, window_end, window_start),
        ).fetchall()
        return {
            "by_provider": [dict(r) for r in rows],
            "total_billed_usd": float(sum((r["billed"] or 0.0) for r in rows)),
        }
    finally:
        conn.close()
