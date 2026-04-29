"""Per-tenant monthly delivery quota tracking.

Adds a `tenant_quotas` table to the existing webhookd SQLite database
and exposes three helpers used by the API and dispatcher:

    get_quota(conn, tenant_id)       -> dict
    set_quota(conn, tenant_id, limit) -> None
    try_consume_quota(conn, tenant_id, n=1) -> bool

A `monthly_limit` of 0 means "unlimited" and consumption never rejects.
The period rolls over after PERIOD_SECONDS (31 days); rolls happen
lazily on the next get/consume.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

PERIOD_SECONDS = 31 * 86_400


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenant_quotas (
    tenant_id     TEXT PRIMARY KEY,
    monthly_limit INTEGER NOT NULL,
    current_count INTEGER NOT NULL DEFAULT 0,
    period_start  INTEGER NOT NULL
);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def _get_row(conn: sqlite3.Connection, tenant_id: str) -> dict[str, Any]:
    _ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM tenant_quotas WHERE tenant_id = ?", (tenant_id,),
    ).fetchone()
    if row is None:
        now = int(time.time())
        conn.execute(
            "INSERT INTO tenant_quotas (tenant_id, monthly_limit, current_count, period_start) "
            "VALUES (?, 0, 0, ?)",
            (tenant_id, now),
        )
        conn.commit()
        return {
            "tenant_id":     tenant_id,
            "monthly_limit": 0,
            "current_count": 0,
            "period_start":  now,
        }
    return dict(row)


def _maybe_rollover(conn: sqlite3.Connection, row: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    if now - row["period_start"] >= PERIOD_SECONDS:
        conn.execute(
            "UPDATE tenant_quotas SET current_count = 0, period_start = ? "
            "WHERE tenant_id = ?",
            (now, row["tenant_id"]),
        )
        conn.commit()
        row = dict(row)
        row["current_count"] = 0
        row["period_start"] = now
    return row


def _remaining(row: dict[str, Any]) -> int | None:
    limit = row["monthly_limit"]
    if limit <= 0:
        return None
    return max(0, limit - row["current_count"])


def get_quota(conn: sqlite3.Connection, tenant_id: str) -> dict[str, Any]:
    row = _get_row(conn, tenant_id)
    row = _maybe_rollover(conn, row)
    return {
        "tenant_id":     tenant_id,
        "monthly_limit": row["monthly_limit"],
        "current_count": row["current_count"],
        "period_start":  row["period_start"],
        "remaining":     _remaining(row),
    }


def set_quota(
    conn: sqlite3.Connection, tenant_id: str, monthly_limit: int,
) -> None:
    if monthly_limit < 0:
        raise ValueError("monthly_limit must be >= 0")
    _get_row(conn, tenant_id)
    conn.execute(
        "UPDATE tenant_quotas SET monthly_limit = ? WHERE tenant_id = ?",
        (monthly_limit, tenant_id),
    )
    conn.commit()


def try_consume_quota(
    conn: sqlite3.Connection, tenant_id: str, n: int = 1,
) -> bool:
    if n <= 0:
        return True
    row = _get_row(conn, tenant_id)
    row = _maybe_rollover(conn, row)
    limit = row["monthly_limit"]
    if limit > 0 and row["current_count"] + n > limit:
        return False
    conn.execute(
        "UPDATE tenant_quotas SET current_count = current_count + ? "
        "WHERE tenant_id = ?",
        (n, tenant_id),
    )
    conn.commit()
    return True
