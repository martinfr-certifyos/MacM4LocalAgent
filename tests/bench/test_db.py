"""bench.db: schema bootstrap, record_run, record_provider_spend, list_runs."""
from __future__ import annotations

import time

from bench import db


def test_schema_creates_bench_tables(tmp_db) -> None:
    conn = db.connect()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    for required in ("bench_runs", "bench_summary", "provider_spend",
                     "requests", "comparisons"):
        assert required in names, f"missing table {required}"


def test_record_run_inserts_row(tmp_db) -> None:
    rid = db.record_run({
        "task_id": "lru_ttl_cache",
        "arm": "local-only",
        "model": "local-long",
        "input_tok": 100, "output_tok": 200,
        "wall_ms": 1500, "generate_ms": 1400, "ttft_ms": 250,
        "pytest_passed": 22, "pytest_total": 24,
        "passes_tests": 22 / 24,
        "composite_score": 0.93,
        "raw_metadata": {"prompt_chars": 1234},
    })
    assert rid >= 1
    rows = db.list_runs(task_id="lru_ttl_cache")
    assert len(rows) == 1
    assert rows[0]["arm"] == "local-only"
    assert rows[0]["pytest_passed"] == 22
    assert rows[0]["composite_score"] == 0.93


def test_record_run_requires_keys(tmp_db) -> None:
    import pytest as _pytest
    with _pytest.raises(ValueError):
        db.record_run({"arm": "x", "model": "y"})  # missing task_id


def test_record_provider_spend_and_window_query(tmp_db) -> None:
    db.record_provider_spend({
        "arm": "claude-only",
        "task_id": "t1",
        "window_start": 1000, "window_end": 2000,
        "provider": "anthropic",
        "source": "admin-api",
        "input_tok": 5000, "output_tok": 1000,
        "billed_usd": 0.0,
    })
    db.record_provider_spend({
        "arm": "claude-only",
        "task_id": "t1",
        "window_start": 1000, "window_end": 2000,
        "provider": "anthropic",
        "source": "admin-api-cost",
        "billed_usd": 0.42,
    })
    summary = db.provider_spend_for_window(
        arm="claude-only", window_start=1000, window_end=2000,
    )
    assert summary["total_billed_usd"] == 0.42
    sources = {p["source"] for p in summary["by_provider"]}
    assert sources == {"admin-api", "admin-api-cost"}


def test_provider_spend_window_excludes_other_arms(tmp_db) -> None:
    db.record_provider_spend({
        "arm": "claude-only",
        "window_start": 100, "window_end": 200,
        "provider": "anthropic", "source": "admin-api-cost",
        "billed_usd": 1.0,
    })
    db.record_provider_spend({
        "arm": "cursor-no-proxy",
        "window_start": 100, "window_end": 200,
        "provider": "cursor", "source": "usage-events-rollup",
        "billed_usd": 9.99,
    })
    s_claude = db.provider_spend_for_window(
        arm="claude-only", window_start=100, window_end=200)
    s_cursor = db.provider_spend_for_window(
        arm="cursor-no-proxy", window_start=100, window_end=200)
    assert s_claude["total_billed_usd"] == 1.0
    assert s_cursor["total_billed_usd"] == 9.99
