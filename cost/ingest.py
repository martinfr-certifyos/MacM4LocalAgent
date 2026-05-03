"""Helpers shared between the LiteLLM router callback, the dashboard, and the
A/B comparator. Provides a connection factory + insert helpers, all keyed
against cost/cost.db.
"""

from __future__ import annotations

import pathlib
import sqlite3
import time
from typing import Any

from cost.pricing import (
    actual_claude_cost,
    shadow_cost as _shadow_cost,
    sonnet_rate,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "cost" / "cost.db"
SCHEMA_PATH = REPO_ROOT / "cost" / "schema.sql"

# Backwards-compat shims: a few benchmark drivers still import the
# old per-token constants directly. Re-export them from the canonical
# pricing table (Sonnet 4.6 rates) so old call sites keep working.
# New code should use cost.pricing.claude_rate() / actual_claude_cost().
CLAUDE_INPUT_PER_TOKEN = sonnet_rate().input
CLAUDE_OUTPUT_PER_TOKEN = sonnet_rate().output


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # Order matters: the migration must run BEFORE the schema, because
    # schema.sql declares an index on `task_id` which references a column
    # that may not yet exist on databases predating this commit. The
    # migration is a no-op on fresh databases (the table doesn't exist
    # yet, PRAGMA returns no rows -> nothing to add).
    _migrate_requests_columns(conn)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_requests_columns(conn: sqlite3.Connection) -> None:
    """Idempotent column additions for the `requests` table.

    SQLite's `ALTER TABLE ADD COLUMN` is not protected by IF NOT EXISTS, so
    we introspect the live schema before each ALTER. The CREATE TABLE in
    schema.sql already lists these columns, so for FRESH databases this
    is a no-op (PRAGMA returns no rows when the table doesn't yet exist).
    The migration is here for databases that pre-date a column being
    added; it lets us evolve the schema without forcing users to wipe
    cost.db.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(requests)").fetchall()}
    if not cols:
        # Fresh DB: table doesn't exist yet. CREATE TABLE in schema.sql
        # will set up everything; nothing to migrate.
        return
    for col, ddl in (
        ("task_id",   "ALTER TABLE requests ADD COLUMN task_id   TEXT"),
        ("task_text", "ALTER TABLE requests ADD COLUMN task_text TEXT"),
    ):
        if col not in cols:
            conn.execute(ddl)
    conn.commit()


def shadow_cost(in_tok: int, out_tok: int) -> float:
    """Stable Sonnet-4.6 baseline. Delegates to cost.pricing.shadow_cost
    so the shadow benchmark stays consistent across the codebase even
    if Sonnet's published rate ever changes."""
    return _shadow_cost(in_tok, out_tok)


def claude_cost(model_id: str, in_tok: int, out_tok: int) -> float:
    """Actual cost for a Claude call, looked up by model id.

    Re-export of cost.pricing.actual_claude_cost so existing
    callers can stay on the cost.ingest namespace. Returns 0.0
    when in_tok and out_tok are both zero.
    """
    return actual_claude_cost(model_id, in_tok, out_tok)


def record_request(
    *,
    model: str,
    tier: str,
    in_tok: int,
    out_tok: int,
    actual_cost: float,
    latency_ms: int = 0,
    route_reason: str = "",
    task_id: str | None = None,
    task_text: str | None = None,
    ts: int | None = None,
) -> int:
    """Insert one request row. Returns its rowid.

    `task_id` and `task_text` are populated for Cline traffic (where the
    user's prompt is wrapped in a `<task>...</task>` envelope and the
    router computes a stable fingerprint). For non-Cline traffic, both
    are NULL and the dashboard's task-grouped views simply skip those
    rows -- they aren't part of an agent task by definition.
    """
    conn = connect()
    cur = conn.execute(
        """
        INSERT INTO requests
          (ts, model, tier, input_tok, output_tok, actual_cost, shadow_cost,
           latency_ms, route_reason, task_id, task_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts or int(time.time()),
            model,
            tier,
            in_tok,
            out_tok,
            actual_cost,
            shadow_cost(in_tok, out_tok),
            latency_ms,
            route_reason,
            task_id,
            task_text,
        ),
    )
    conn.commit()
    rid = cur.lastrowid or 0
    conn.close()
    return rid


def record_comparison(row: dict[str, Any]) -> int:
    conn = connect()
    cur = conn.execute(
        """
        INSERT INTO comparisons
          (ts, prompt, local_model, claude_model, local_output, claude_output,
           local_in_tok, local_out_tok, claude_in_tok, claude_out_tok,
           local_cost, claude_cost, local_ms, claude_ms, judge_score)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            row.get("ts", int(time.time())),
            row["prompt"],
            row["local_model"],
            row["claude_model"],
            row.get("local_output", ""),
            row.get("claude_output", ""),
            row.get("local_in_tok", 0),
            row.get("local_out_tok", 0),
            row.get("claude_in_tok", 0),
            row.get("claude_out_tok", 0),
            row.get("local_cost", 0.0),
            row.get("claude_cost", 0.0),
            row.get("local_ms", 0),
            row.get("claude_ms", 0),
            row.get("judge_score", 0.0),
        ),
    )
    conn.commit()
    rid = cur.lastrowid or 0
    conn.close()
    return rid
