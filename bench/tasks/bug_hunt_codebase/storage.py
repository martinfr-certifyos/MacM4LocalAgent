"""Persistent storage layer for the webhook delivery service.

Wraps a SQLite database that stores webhook subscriptions, delivery
attempts, and dead-letter queue rows. All access is synchronous; the
delivery worker calls these from inside thread-pool executors.

Schema:

    CREATE TABLE subscriptions (
        id            TEXT PRIMARY KEY,             -- uuid4
        tenant_id     TEXT NOT NULL,
        url           TEXT NOT NULL,
        secret        TEXT NOT NULL,                -- HMAC signing key
        event_types   TEXT NOT NULL,                -- comma-separated
        active        INTEGER NOT NULL DEFAULT 1,
        created_at    INTEGER NOT NULL,
        updated_at    INTEGER NOT NULL
    );

    CREATE TABLE deliveries (
        id            TEXT PRIMARY KEY,             -- uuid4
        subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
        event_id      TEXT NOT NULL,
        event_type    TEXT NOT NULL,
        payload       TEXT NOT NULL,                -- JSON blob
        attempts      INTEGER NOT NULL DEFAULT 0,
        last_status   INTEGER,                      -- HTTP status of last try
        last_error    TEXT,
        scheduled_at  INTEGER NOT NULL,             -- unix sec next attempt
        delivered_at  INTEGER,                      -- null until success
        created_at    INTEGER NOT NULL
    );

    CREATE TABLE dead_letter (
        id            TEXT PRIMARY KEY,
        delivery_id   TEXT NOT NULL,
        reason        TEXT NOT NULL,
        moved_at      INTEGER NOT NULL,
        original_payload TEXT NOT NULL
    );

The functions in this module return ordinary Python dicts (decoded from
JSON where appropriate); they never expose sqlite3.Row objects to the
caller because the higher layers serialize results out as JSON for the
HTTP API.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

DB_PATH = "/var/lib/webhookd/webhookd.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id            TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    url           TEXT NOT NULL,
    secret        TEXT NOT NULL,
    event_types   TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id            TEXT PRIMARY KEY,
    subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
    event_id      TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    payload       TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_status   INTEGER,
    last_error    TEXT,
    scheduled_at  INTEGER NOT NULL,
    delivered_at  INTEGER,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dead_letter (
    id            TEXT PRIMARY KEY,
    delivery_id   TEXT NOT NULL,
    reason        TEXT NOT NULL,
    moved_at      INTEGER NOT NULL,
    original_payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_subs_tenant ON subscriptions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_due
    ON deliveries(scheduled_at, delivered_at);
"""


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Open the SQLite DB and ensure the schema is in place."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def create_subscription(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    url: str,
    secret: str,
    event_types: list[str],
) -> dict[str, Any]:
    """Insert a new subscription and return the full record."""
    sid = str(uuid.uuid4())
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO subscriptions
            (id, tenant_id, url, secret, event_types, active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (sid, tenant_id, url, secret, ",".join(event_types), now, now),
    )
    conn.commit()
    return get_subscription(conn, sid)


def get_subscription(conn: sqlite3.Connection, sid: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM subscriptions WHERE id = ?", (sid,)
    ).fetchone()
    if row is None:
        raise KeyError(sid)
    d = dict(row)
    d["event_types"] = d["event_types"].split(",") if d["event_types"] else []
    d["active"] = bool(d["active"])
    return d


def list_subscriptions_for_tenant(
    conn: sqlite3.Connection, tenant_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM subscriptions WHERE tenant_id = ? ORDER BY created_at DESC",
        (tenant_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["event_types"] = d["event_types"].split(",") if d["event_types"] else []
        d["active"] = bool(d["active"])
        out.append(d)
    return out


def deactivate_subscription(conn: sqlite3.Connection, sid: str) -> None:
    """Mark a subscription inactive so the dispatcher will skip it."""
    now = int(time.time())
    conn.execute(
        "UPDATE subscriptions SET active = 0, updated_at = ? WHERE id = ?",
        (now, sid),
    )
    conn.commit()


def enqueue_delivery(
    conn: sqlite3.Connection,
    *,
    subscription_id: str,
    event_id: str,
    event_type: str,
    payload: dict[str, Any],
    scheduled_at: int | None = None,
) -> str:
    """Schedule a webhook delivery for a single subscriber.

    Returns the delivery id. The dispatcher polls deliveries with
    `delivered_at IS NULL AND scheduled_at <= now` and tries each one.
    """
    did = str(uuid.uuid4())
    now = int(time.time())
    sched = scheduled_at if scheduled_at is not None else now
    conn.execute(
        """
        INSERT INTO deliveries
            (id, subscription_id, event_id, event_type, payload,
             attempts, scheduled_at, created_at)
        VALUES (?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            did, subscription_id, event_id, event_type,
            json.dumps(payload), sched, now,
        ),
    )
    conn.commit()
    return did


def claim_due_deliveries(
    conn: sqlite3.Connection, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Return up to `limit` deliveries that are due to be tried.

    Note: this does NOT lock the rows; the dispatcher relies on the
    fact that each delivery has only one subscription_id, and that the
    SQLite database is written from a single process. Multi-process
    deployments would need to add a `claimed_by` column.
    """
    now = int(time.time())
    rows = conn.execute(
        """
        SELECT * FROM deliveries
        WHERE delivered_at IS NULL
          AND scheduled_at <= ?
        ORDER BY scheduled_at ASC
        LIMIT ?
        """,
        (now, limit),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        out.append(d)
    return out


def record_attempt(
    conn: sqlite3.Connection,
    delivery_id: str,
    *,
    status_code: int | None,
    error: str | None,
    delivered: bool,
    next_scheduled_at: int | None = None,
) -> None:
    """Update a delivery row after an HTTP attempt."""
    now = int(time.time())
    if delivered:
        conn.execute(
            """
            UPDATE deliveries
            SET attempts = attempts + 1,
                last_status = ?,
                last_error = NULL,
                delivered_at = ?
            WHERE id = ?
            """,
            (status_code, now, delivery_id),
        )
    else:
        conn.execute(
            """
            UPDATE deliveries
            SET attempts = attempts + 1,
                last_status = ?,
                last_error = ?,
                scheduled_at = ?
            WHERE id = ?
            """,
            (status_code, error, next_scheduled_at or now, delivery_id),
        )
    conn.commit()


def move_to_dead_letter(
    conn: sqlite3.Connection, delivery_id: str, reason: str
) -> None:
    """Move a delivery that has exhausted its retries to the DLQ table."""
    row = conn.execute(
        "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)
    ).fetchone()
    if row is None:
        return
    dlq_id = str(uuid.uuid4())
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO dead_letter
            (id, delivery_id, reason, moved_at, original_payload)
        VALUES (?, ?, ?, ?, ?)
        """,
        (dlq_id, delivery_id, reason, now, row["payload"]),
    )
    conn.commit()


def count_attempts_in_window(
    conn: sqlite3.Connection,
    subscription_id: str,
    *,
    window_seconds: int = 60,
) -> int:
    """Count delivery attempts for a subscription in the last window.

    Used by the rate limiter to throttle a single misbehaving endpoint.
    """
    cutoff = int(time.time()) - window_seconds
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM deliveries
        WHERE subscription_id = ?
          AND created_at >= ?
        """,
        (subscription_id, cutoff),
    ).fetchone()
    return int(row["n"])
