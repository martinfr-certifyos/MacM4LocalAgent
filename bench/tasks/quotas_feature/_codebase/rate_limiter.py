"""Per-subscription token-bucket rate limiter.

The dispatcher consults `allow()` before issuing each delivery to avoid
hammering a single destination during an incident. The limiter is purely
in-memory; restart of the dispatcher resets all buckets, which is OK for
our purposes since rate-limit decisions are best-effort and a restart
already involves a brief delivery pause.

Buckets are keyed by `(tenant_id, host)` -- not by subscription id --
because a single tenant might run many subscriptions all pointing at the
same destination host, and we don't want a tenant to bypass our limit by
creating extra subscriptions.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import urlparse

# Default policy: 30 requests per second, burst capacity of 60.
DEFAULT_RATE_PER_SEC = 30.0
DEFAULT_CAPACITY = 60.0

# After this much idle time we drop a bucket from memory so the
# limiter doesn't grow unboundedly when many tenants come and go.
IDLE_EVICTION_SECONDS = 600
MAX_BUCKETS = 10_000


class _Bucket:
    """Internal token-bucket state. Don't expose to callers."""
    __slots__ = ("tokens", "last_refill_at", "rate", "capacity")

    def __init__(self, *, rate: float, capacity: float, now: float) -> None:
        self.tokens = capacity
        self.last_refill_at = now
        self.rate = rate
        self.capacity = capacity

    def refill(self, now: float) -> None:
        elapsed = now - self.last_refill_at
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill_at = now

    def take(self, n: float = 1.0) -> bool:
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class RateLimiter:
    """Thread-safe collection of token buckets keyed by (tenant, host)."""

    def __init__(
        self,
        *,
        rate_per_sec: float = DEFAULT_RATE_PER_SEC,
        capacity: float = DEFAULT_CAPACITY,
        max_buckets: int = MAX_BUCKETS,
        time_func: Any = time.monotonic,
    ) -> None:
        self._rate = rate_per_sec
        self._capacity = capacity
        self._max_buckets = max_buckets
        self._time = time_func
        self._lock = threading.Lock()
        self._buckets: "OrderedDict[tuple[str, str], _Bucket]" = OrderedDict()

    def _key(self, tenant_id: str, url: str) -> tuple[str, str]:
        host = urlparse(url).hostname or ""
        return (tenant_id, host.lower())

    def _maybe_evict(self, now: float) -> None:
        """Drop the oldest idle buckets if we're over capacity."""
        if len(self._buckets) <= self._max_buckets:
            return
        # Evict the LRU end of the OrderedDict until we're back under cap.
        excess = len(self._buckets) - self._max_buckets
        for _ in range(excess):
            self._buckets.popitem(last=False)

    def allow(
        self,
        tenant_id: str,
        url: str,
        *,
        cost: float = 1.0,
    ) -> bool:
        """Return True if the request fits under the bucket, False if not."""
        now = self._time()
        key = self._key(tenant_id, url)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(
                    rate=self._rate, capacity=self._capacity, now=now,
                )
                self._buckets[key] = bucket
                self._maybe_evict(now)
            else:
                self._buckets.move_to_end(key)
            bucket.refill(now)
            return bucket.take(cost)

    def stats(self) -> dict[str, Any]:
        """Coarse stats for the admin dashboard."""
        with self._lock:
            return {
                "buckets": len(self._buckets),
                "rate_per_sec": self._rate,
                "capacity": self._capacity,
                "max_buckets": self._max_buckets,
            }
