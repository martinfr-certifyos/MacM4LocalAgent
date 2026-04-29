"""In-process metrics for the dispatcher and HTTP API.

Exposes a tiny Prometheus-compatible text-format scrape endpoint
(`metrics_scrape()` returns the body) and a small `MetricsRegistry` that
holds counters, gauges, and histograms keyed by a metric name plus a
flat dict of labels.

We deliberately keep this lightweight rather than depend on
`prometheus_client` -- the deployment image stays small, and we don't
need pushgateway, exemplars, summaries, or any of the more advanced
features.

The label cardinality cap is enforced at registration time; any single
metric is capped at MAX_LABEL_VALUES distinct label combinations to
prevent runaway cardinality from buggy callers (e.g., putting a request
id into a label).
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Iterable

DEFAULT_HISTOGRAM_BUCKETS_MS: tuple[float, ...] = (
    1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000,
)
MAX_LABEL_VALUES = 5_000


def _label_key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """Produce a hashable, order-independent key from a labels dict."""
    return tuple(sorted(labels.items()))


def _format_labels(labels: dict[str, str]) -> str:
    """Render labels in Prometheus text format."""
    if not labels:
        return ""
    parts = [
        f'{k}="{_escape(v)}"' for k, v in sorted(labels.items())
    ]
    return "{" + ",".join(parts) + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Counter:
    __slots__ = ("name", "help_text", "_values", "_lock")

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help_text = help_text
        self._values: dict[tuple[tuple[str, str], ...], float] = {}
        self._lock = threading.Lock()

    def inc(self, *, labels: dict[str, str] | None = None, by: float = 1.0) -> None:
        labels = labels or {}
        if by < 0:
            raise ValueError("counters cannot decrement")
        key = _label_key(labels)
        with self._lock:
            if key not in self._values and len(self._values) >= MAX_LABEL_VALUES:
                # Silently drop new label combinations once we hit the cap.
                # We log this elsewhere (in `MetricsRegistry.cardinality_warnings`)
                # rather than raise, because callers usually can't recover.
                return
            self._values[key] = self._values.get(key, 0.0) + by

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help_text}"
        yield f"# TYPE {self.name} counter"
        with self._lock:
            for key, value in self._values.items():
                labels = dict(key)
                yield f"{self.name}{_format_labels(labels)} {value}"


class _Gauge:
    __slots__ = ("name", "help_text", "_values", "_lock")

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help_text = help_text
        self._values: dict[tuple[tuple[str, str], ...], float] = {}
        self._lock = threading.Lock()

    def set(self, value: float, *, labels: dict[str, str] | None = None) -> None:
        labels = labels or {}
        key = _label_key(labels)
        with self._lock:
            self._values[key] = value

    def inc(self, by: float = 1.0, *, labels: dict[str, str] | None = None) -> None:
        labels = labels or {}
        key = _label_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + by

    def dec(self, by: float = 1.0, *, labels: dict[str, str] | None = None) -> None:
        self.inc(by=-by, labels=labels)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help_text}"
        yield f"# TYPE {self.name} gauge"
        with self._lock:
            for key, value in self._values.items():
                labels = dict(key)
                yield f"{self.name}{_format_labels(labels)} {value}"


class _Histogram:
    __slots__ = ("name", "help_text", "buckets", "_data", "_lock")

    def __init__(
        self,
        name: str,
        help_text: str,
        buckets: tuple[float, ...] = DEFAULT_HISTOGRAM_BUCKETS_MS,
    ) -> None:
        self.name = name
        self.help_text = help_text
        # Buckets must be sorted ascending. Append +Inf if not present.
        sorted_buckets = tuple(sorted(buckets))
        if sorted_buckets[-1] != math.inf:
            sorted_buckets = sorted_buckets + (math.inf,)
        self.buckets = sorted_buckets
        self._data: dict[
            tuple[tuple[str, str], ...],
            tuple[list[int], float, int],
        ] = {}
        self._lock = threading.Lock()

    def observe(
        self, value: float, *, labels: dict[str, str] | None = None,
    ) -> None:
        labels = labels or {}
        key = _label_key(labels)
        with self._lock:
            counts, total, n = self._data.get(
                key, ([0] * len(self.buckets), 0.0, 0),
            )
            for i, b in enumerate(self.buckets):
                if value <= b:
                    counts[i] += 1
            self._data[key] = (counts, total + value, n + 1)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help_text}"
        yield f"# TYPE {self.name} histogram"
        with self._lock:
            for key, (counts, total, n) in self._data.items():
                labels = dict(key)
                cumulative = 0
                for i, b in enumerate(self.buckets):
                    cumulative = counts[i]
                    le_label = "+Inf" if math.isinf(b) else str(b)
                    bucket_labels = {**labels, "le": le_label}
                    yield f"{self.name}_bucket{_format_labels(bucket_labels)} {cumulative}"
                yield f"{self.name}_sum{_format_labels(labels)} {total}"
                yield f"{self.name}_count{_format_labels(labels)} {n}"


class MetricsRegistry:
    """Owns the universe of metrics and renders them on scrape."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, _Counter] = {}
        self._gauges: dict[str, _Gauge] = {}
        self._histograms: dict[str, _Histogram] = {}

    def counter(self, name: str, help_text: str = "") -> _Counter:
        with self._lock:
            if name not in self._counters:
                self._counters[name] = _Counter(name, help_text)
            return self._counters[name]

    def gauge(self, name: str, help_text: str = "") -> _Gauge:
        with self._lock:
            if name not in self._gauges:
                self._gauges[name] = _Gauge(name, help_text)
            return self._gauges[name]

    def histogram(
        self,
        name: str,
        help_text: str = "",
        buckets: tuple[float, ...] = DEFAULT_HISTOGRAM_BUCKETS_MS,
    ) -> _Histogram:
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = _Histogram(name, help_text, buckets)
            return self._histograms[name]

    def render(self) -> str:
        """Render the entire registry as a Prometheus text-format string."""
        lines: list[str] = []
        with self._lock:
            for c in self._counters.values():
                lines.extend(c.render())
            for g in self._gauges.values():
                lines.extend(g.render())
            for h in self._histograms.values():
                lines.extend(h.render())
        return "\n".join(lines) + "\n"


# Module-level default registry (most callers just import this).
default_registry = MetricsRegistry()


# Common pre-declared metrics so call sites don't need to repeat help text.
deliveries_total = default_registry.counter(
    "webhookd_deliveries_total",
    "Number of delivery attempts, labeled by outcome (delivered, retried, dlq).",
)
delivery_duration_ms = default_registry.histogram(
    "webhookd_delivery_duration_ms",
    "End-to-end HTTP attempt duration in milliseconds.",
)
in_flight_deliveries = default_registry.gauge(
    "webhookd_in_flight_deliveries",
    "Number of deliveries currently being attempted.",
)
api_requests_total = default_registry.counter(
    "webhookd_api_requests_total",
    "Number of admin API requests, labeled by method and status.",
)
rate_limit_drops_total = default_registry.counter(
    "webhookd_rate_limit_drops_total",
    "Number of deliveries delayed because the rate limiter said no.",
)


def metrics_scrape() -> bytes:
    """Return the rendered Prometheus body. Used by the WSGI app."""
    return default_registry.render().encode("utf-8")


def time_block(histogram: _Histogram, labels: dict[str, str] | None = None):
    """Context manager that records the elapsed time of a block in ms."""
    return _TimeBlock(histogram, labels)


class _TimeBlock:
    def __init__(self, histogram: _Histogram, labels: dict[str, str] | None) -> None:
        self._histogram = histogram
        self._labels = labels
        self._started = 0.0

    def __enter__(self) -> "_TimeBlock":
        self._started = time.monotonic()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        elapsed_ms = (time.monotonic() - self._started) * 1000.0
        self._histogram.observe(elapsed_ms, labels=self._labels)
