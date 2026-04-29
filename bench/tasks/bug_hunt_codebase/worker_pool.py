"""Bounded thread-pool wrapper used by the dispatcher.

The standard `concurrent.futures.ThreadPoolExecutor` has two issues for
our workload:

    1. Submissions block forever when the pool is overloaded (its queue
       is unbounded). We want push-back: when the queue exceeds a high-
       water mark, callers should get an immediate "busy" signal so the
       dispatcher can pause polling instead of allocating unboundedly.
    2. We want clean shutdown semantics: `shutdown(wait=True)` should
       drain in-flight work but cancel queued work, so a SIGTERM during
       a deploy doesn't sit on hundreds of queued retries.

This module provides a small wrapper that adds a queue depth limit and
a `shutdown(drain=True)` helper.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)


class WorkerBusy(RuntimeError):
    """Raised by `submit()` when the queue is at the high-water mark."""


class WorkerPool:
    """Bounded thread pool. Tasks are `(callable, args, kwargs)` tuples."""

    def __init__(
        self,
        *,
        size: int,
        max_queue_depth: int,
        name_prefix: str = "worker",
    ) -> None:
        if size <= 0:
            raise ValueError("size must be >= 1")
        if max_queue_depth <= 0:
            raise ValueError("max_queue_depth must be >= 1")
        self._size = size
        self._max_queue_depth = max_queue_depth
        self._name_prefix = name_prefix
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=max_queue_depth)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._started = False
        self._stats_lock = threading.Lock()
        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._rejected = 0

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for i in range(self._size):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"{self._name_prefix}-{i}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

    def submit(
        self, fn: Callable[..., Any], *args: Any, **kwargs: Any,
    ) -> None:
        """Enqueue a task. Raises WorkerBusy if the queue is full."""
        if not self._started:
            raise RuntimeError("WorkerPool.start() before submit()")
        if self._stop.is_set():
            raise RuntimeError("WorkerPool is shutting down")
        try:
            self._queue.put_nowait((fn, args, kwargs))
        except queue.Full:
            with self._stats_lock:
                self._rejected += 1
            raise WorkerBusy(
                f"queue depth {self._queue.qsize()} >= "
                f"{self._max_queue_depth}"
            )
        with self._stats_lock:
            self._submitted += 1

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            fn, args, kwargs = item
            try:
                fn(*args, **kwargs)
                with self._stats_lock:
                    self._completed += 1
            except Exception as e:  # noqa: BLE001
                log.exception("worker task raised: %s", e)
                with self._stats_lock:
                    self._failed += 1
            finally:
                self._queue.task_done()

    def shutdown(self, *, drain: bool = True, timeout: float = 30.0) -> None:
        """Stop accepting new tasks. If `drain` is True wait for the queue
        to fully empty; otherwise cancel queued (but not in-flight) work."""
        self._stop.set()
        if drain:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._queue.unfinished_tasks == 0:
                    break
                time.sleep(0.05)
        else:
            # Drain the queue without running the items.
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads.clear()
        self._started = False

    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return {
                "submitted": self._submitted,
                "completed": self._completed,
                "failed":    self._failed,
                "rejected":  self._rejected,
                "queued":    self._queue.qsize(),
                "size":      self._size,
            }
