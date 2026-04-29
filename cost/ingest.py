"""Helpers shared between the LiteLLM router callback, the dashboard, and the
A/B comparator. Provides a connection factory + insert helpers, all keyed
against cost/cost.db.
"""

from __future__ import annotations

import pathlib
import sqlite3
import time
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "cost" / "cost.db"
SCHEMA_PATH = REPO_ROOT / "cost" / "schema.sql"

CLAUDE_INPUT_PER_TOKEN = 3.0 / 1_000_000
CLAUDE_OUTPUT_PER_TOKEN = 15.0 / 1_000_000


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.row_factory = sqlite3.Row
    return conn


def shadow_cost(in_tok: int, out_tok: int) -> float:
    return in_tok * CLAUDE_INPUT_PER_TOKEN + out_tok * CLAUDE_OUTPUT_PER_TOKEN


def record_request(
    *,
    model: str,
    tier: str,
    in_tok: int,
    out_tok: int,
    actual_cost: float,
    latency_ms: int = 0,
    route_reason: str = "",
    ts: int | None = None,
) -> int:
    """Insert one request row. Returns its rowid."""
    conn = connect()
    cur = conn.execute(
        """
        INSERT INTO requests
          (ts, model, tier, input_tok, output_tok, actual_cost, shadow_cost, latency_ms, route_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
