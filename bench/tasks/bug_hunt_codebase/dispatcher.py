"""Background dispatcher that delivers queued webhooks.

The dispatcher runs in a single dedicated thread and polls the
deliveries table on a fixed interval. For each row that is due, it:

    1. loads the subscription
    2. signs the payload using the subscription's secret
    3. POSTs the body to the destination URL
    4. updates the delivery row with the outcome
    5. either reschedules a retry (with exponential backoff) or, if the
       attempt count has reached MAX_ATTEMPTS, moves the row into the
       dead-letter queue

The exponential backoff doubles the delay each attempt with a small
amount of jitter so a thundering herd of stuck deliveries doesn't all
come back at once when a destination recovers.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any

from . import http_client, signing, storage

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1.0
MAX_ATTEMPTS = 10
BASE_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 3600


def _backoff_delay(attempts: int) -> int:
    """Return how many seconds to wait before the next try.

    `attempts` is the count *including* the just-failed attempt, so on
    the first failure we use `attempts == 1`.
    """
    raw = BASE_BACKOFF_SECONDS * (2 ** (attempts - 1))
    capped = min(raw, MAX_BACKOFF_SECONDS)
    jitter = random.uniform(0.0, capped * 0.1)
    return int(capped + jitter)


def _try_one(conn, delivery: dict[str, Any]) -> None:
    """Attempt a single delivery and update the database.

    `delivery` is the dict returned by `storage.claim_due_deliveries`.
    """
    sub_id = delivery["subscription_id"]
    try:
        sub = storage.get_subscription(conn, sub_id)
    except KeyError:
        log.warning("delivery %s references missing subscription %s",
                    delivery["id"], sub_id)
        storage.move_to_dead_letter(
            conn, delivery["id"], reason="subscription_missing",
        )
        return

    if not sub["active"]:
        log.info("skipping delivery for inactive subscription %s", sub_id)
        storage.move_to_dead_letter(
            conn, delivery["id"], reason="subscription_inactive",
        )
        return

    timestamp = int(time.time())
    sig = signing.sign(sub["secret"], timestamp, delivery["payload"])
    headers = {
        "X-Webhook-Signature": sig,
        "X-Webhook-Timestamp": str(timestamp),
        "X-Webhook-Event-Id": delivery["event_id"],
        "X-Webhook-Event-Type": delivery["event_type"],
        "X-Webhook-Subscription-Id": sub["id"],
    }

    result = http_client.deliver(
        url=sub["url"],
        payload=delivery["payload"],
        headers=headers,
    )

    if result.error is None and result.status_code and 200 <= result.status_code < 300:
        storage.record_attempt(
            conn, delivery["id"],
            status_code=result.status_code,
            error=None,
            delivered=True,
        )
        log.info("delivered %s in %d ms", delivery["id"], result.elapsed_ms)
        return

    attempts = delivery["attempts"] + 1
    if not http_client.is_retryable(result) and attempts >= MAX_ATTEMPTS:
        storage.move_to_dead_letter(
            conn, delivery["id"],
            reason=f"non_retryable_after_{attempts}_attempts: {result.error}",
        )
        return

    if attempts >= MAX_ATTEMPTS:
        storage.move_to_dead_letter(
            conn, delivery["id"],
            reason=f"max_attempts_reached: last_error={result.error}",
        )
        return

    delay = _backoff_delay(attempts)
    next_at = int(time.time()) + delay
    storage.record_attempt(
        conn, delivery["id"],
        status_code=result.status_code,
        error=result.error or f"HTTP {result.status_code}",
        delivered=False,
        next_scheduled_at=next_at,
    )
    log.info(
        "delivery %s failed (attempt %d/%d, status=%s); retry in %ds",
        delivery["id"], attempts, MAX_ATTEMPTS, result.status_code, delay,
    )


def _tick(conn) -> int:
    """Process one batch of due deliveries. Returns the count tried."""
    due = storage.claim_due_deliveries(conn, limit=50)
    for d in due:
        try:
            _try_one(conn, d)
        except Exception as e:
            log.exception("dispatcher exception on delivery %s: %s",
                          d.get("id"), e)
    return len(due)


class Dispatcher:
    """Owns the background polling thread."""

    def __init__(self, db_path: str = storage.DB_PATH) -> None:
        self._db_path = db_path
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: Any = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="webhookd-dispatcher", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        # Each thread needs its own SQLite connection.
        self._conn = storage.connect(self._db_path)
        try:
            while not self._stop.is_set():
                tried = _tick(self._conn)
                if tried == 0:
                    time.sleep(POLL_INTERVAL_SECONDS)
        finally:
            if self._conn is not None:
                self._conn.close()


def deliver_now(
    conn,
    *,
    subscription_id: str,
    event_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> str:
    """Convenience: enqueue a delivery and immediately try it once.

    Used by the admin "test webhook" button so a tenant can verify
    their endpoint without waiting for the next dispatcher tick.
    """
    did = storage.enqueue_delivery(
        conn,
        subscription_id=subscription_id,
        event_id=event_id,
        event_type=event_type,
        payload=payload,
    )
    sub = storage.get_subscription(conn, subscription_id)
    timestamp = int(time.time())
    sig = signing.sign(sub["secret"], timestamp, payload)
    headers = {
        "X-Webhook-Signature": sig,
        "X-Webhook-Timestamp": str(timestamp),
        "X-Webhook-Event-Id": event_id,
        "X-Webhook-Event-Type": event_type,
    }
    result = http_client.deliver(
        url=sub["url"], payload=payload, headers=headers,
    )
    if result.error is None and result.status_code and 200 <= result.status_code < 300:
        storage.record_attempt(
            conn, did, status_code=result.status_code,
            error=None, delivered=True,
        )
    else:
        storage.record_attempt(
            conn, did, status_code=result.status_code,
            error=result.error or f"HTTP {result.status_code}",
            delivered=False,
        )
    return did
