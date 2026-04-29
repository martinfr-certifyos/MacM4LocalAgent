"""Grader test suite for the `lru_ttl_cache` benchmark task.

This file is copied next to the model's submitted `lru_ttl_cache.py` and run
under pytest with `--tb=short -q --json-report`. We tally passing tests as the
correctness signal.

DO NOT EDIT to make a particular model pass. These tests define the contract
the prompt asks for. If you want a different contract, write a different task.
"""
from __future__ import annotations

import threading
import time

import pytest

import lru_ttl_cache as M


# ---- structural --------------------------------------------------------------

def test_module_has_docstring_with_design_notes() -> None:
    doc = (M.__doc__ or "").strip()
    assert len(doc.split()) >= 60, "module docstring must explain design choices (>=60 words)"


def test_exports_lrucache_class_and_memoize_decorator() -> None:
    assert hasattr(M, "LRUTTLCache")
    assert hasattr(M, "memoize")


def test_no_third_party_imports() -> None:
    import ast
    import pathlib
    src = pathlib.Path(M.__file__).read_text()  # type: ignore[arg-type]
    tree = ast.parse(src)
    allowed_prefixes = {
        "collections", "threading", "time", "functools", "typing",
        "dataclasses", "weakref", "heapq", "math", "operator",
        "__future__", "abc", "sys",
    }
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                root = n.name.split(".")[0]
                if root not in allowed_prefixes:
                    bad.append(n.name)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root and root not in allowed_prefixes:
                bad.append(node.module or "")
    assert not bad, f"unexpected imports: {bad}"


# ---- LRUTTLCache: basic ------------------------------------------------------

def test_set_and_get() -> None:
    c = M.LRUTTLCache(maxsize=4)
    c.set("a", 1)
    assert c.get("a") == 1
    assert c.get("missing") is None
    assert c.get("missing", "default") == "default"


def test_len_and_contains() -> None:
    c = M.LRUTTLCache(maxsize=4)
    c.set("a", 1)
    c.set("b", 2)
    assert len(c) == 2
    assert "a" in c
    assert "z" not in c


def test_delete_returns_bool() -> None:
    c = M.LRUTTLCache(maxsize=4)
    c.set("a", 1)
    assert c.delete("a") is True
    assert c.delete("a") is False
    assert "a" not in c


def test_clear() -> None:
    c = M.LRUTTLCache(maxsize=4)
    c.set("a", 1); c.set("b", 2)
    c.clear()
    assert len(c) == 0


# ---- LRUTTLCache: LRU semantics ---------------------------------------------

def test_lru_eviction_on_overflow() -> None:
    c = M.LRUTTLCache(maxsize=2)
    c.set("a", 1); c.set("b", 2)
    c.set("c", 3)  # should evict 'a'
    assert "a" not in c
    assert "b" in c and "c" in c


def test_get_refreshes_recency() -> None:
    c = M.LRUTTLCache(maxsize=2)
    c.set("a", 1); c.set("b", 2)
    _ = c.get("a")    # 'a' becomes most-recent
    c.set("c", 3)     # should evict 'b', not 'a'
    assert "a" in c
    assert "b" not in c
    assert "c" in c


def test_set_existing_key_refreshes_recency() -> None:
    c = M.LRUTTLCache(maxsize=2)
    c.set("a", 1); c.set("b", 2)
    c.set("a", 11)    # touches 'a'
    c.set("c", 3)     # should evict 'b'
    assert c.get("a") == 11
    assert "b" not in c


# ---- LRUTTLCache: TTL semantics ---------------------------------------------

def test_default_ttl_expires_lazily() -> None:
    fake = {"now": 1000.0}
    c = M.LRUTTLCache(maxsize=8, default_ttl=10.0, time_func=lambda: fake["now"])
    c.set("a", 1)
    fake["now"] = 1009.99
    assert c.get("a") == 1
    fake["now"] = 1010.01
    assert c.get("a") is None


def test_per_call_ttl_overrides_default() -> None:
    fake = {"now": 1000.0}
    c = M.LRUTTLCache(maxsize=8, default_ttl=10.0, time_func=lambda: fake["now"])
    c.set("short", 1, ttl=1.0)
    c.set("long",  2, ttl=100.0)
    fake["now"] = 1002.0
    assert c.get("short") is None
    assert c.get("long") == 2


def test_ttl_none_means_no_expiry() -> None:
    fake = {"now": 1000.0}
    c = M.LRUTTLCache(maxsize=8, default_ttl=None, time_func=lambda: fake["now"])
    c.set("a", 1)
    fake["now"] = 1e9
    assert c.get("a") == 1


def test_contains_respects_expiry() -> None:
    fake = {"now": 1000.0}
    c = M.LRUTTLCache(maxsize=8, default_ttl=1.0, time_func=lambda: fake["now"])
    c.set("a", 1)
    fake["now"] = 1002.0
    assert "a" not in c


# ---- LRUTTLCache: stats ------------------------------------------------------

def test_stats_counts_hits_misses_evictions_expirations() -> None:
    fake = {"now": 1000.0}
    c = M.LRUTTLCache(maxsize=2, default_ttl=10.0, time_func=lambda: fake["now"])
    c.set("a", 1); c.set("b", 2)
    c.get("a")          # hit
    c.get("a")          # hit
    c.get("missing")    # miss
    c.set("c", 3)       # evict 'b'
    fake["now"] = 1100.0
    c.get("a")          # expired -> counts as miss + expiration

    s = c.stats()
    assert s["hits"] == 2
    assert s["misses"] >= 1            # at least 'missing'
    assert s["evictions"] >= 1
    assert s["expirations"] >= 1
    assert s["size"] == len(c)


# ---- LRUTTLCache: thread safety ---------------------------------------------

def test_thread_safety_smoke() -> None:
    c = M.LRUTTLCache(maxsize=200)
    errors: list[BaseException] = []

    def worker(start: int) -> None:
        try:
            for i in range(start, start + 500):
                c.set(i, i * 2)
                _ = c.get(i)
                if i % 7 == 0:
                    c.delete(i)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(s * 1000,)) for s in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors, f"thread workers raised: {errors[:3]}"
    assert len(c) <= 200


# ---- memoize decorator -------------------------------------------------------

def test_memoize_basic_caching() -> None:
    calls = {"n": 0}

    @M.memoize(maxsize=8)
    def slow(x: int) -> int:
        calls["n"] += 1
        return x * x

    assert slow(3) == 9
    assert slow(3) == 9
    assert calls["n"] == 1


def test_memoize_distinguishes_kwargs() -> None:
    calls = {"n": 0}

    @M.memoize(maxsize=8)
    def f(a: int, *, b: int = 1) -> int:
        calls["n"] += 1
        return a + b

    f(1, b=2); f(1, b=2); f(1, b=3)
    assert calls["n"] == 2


def test_memoize_cache_info_and_clear() -> None:
    @M.memoize(maxsize=4)
    def f(x: int) -> int: return x

    f(1); f(1); f(2)
    info = f.cache_info()
    assert info.hits == 1
    assert info.misses == 2
    assert info.maxsize == 4
    assert info.currsize == 2
    f.cache_clear()
    assert f.cache_info().currsize == 0


def test_memoize_preserves_metadata() -> None:
    @M.memoize()
    def greet(name: str) -> str:
        """Say hello."""
        return f"hi {name}"
    assert greet.__name__ == "greet"
    assert greet.__doc__ == "Say hello."
    assert hasattr(greet, "__wrapped__")


def test_memoize_ttl_expires() -> None:
    fake = {"now": 0.0}

    @M.memoize(maxsize=4, ttl=5.0)
    def f(x: int) -> float:
        return fake["now"] + x

    # Patch the module's time_func indirection if exposed; otherwise fall back
    # to monkeypatching time.monotonic temporarily.
    import time as _time
    real = _time.monotonic
    _time.monotonic = lambda: fake["now"]  # type: ignore[assignment]
    try:
        v1 = f(0)
        fake["now"] = 4.0
        v2 = f(0)
        assert v1 == v2          # still cached
        fake["now"] = 6.0
        v3 = f(0)
        assert v3 != v1          # expired -> recomputed
    finally:
        _time.monotonic = real  # type: ignore[assignment]


# ---- type hints (light check) ------------------------------------------------

def test_public_callables_have_annotations() -> None:
    import inspect
    cls = M.LRUTTLCache
    for name in ("set", "get", "delete", "clear", "stats"):
        sig = inspect.signature(getattr(cls, name))
        assert sig.return_annotation is not inspect.Signature.empty, f"{name} missing return type"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
