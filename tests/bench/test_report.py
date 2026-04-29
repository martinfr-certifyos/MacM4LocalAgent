"""bench.report.summarize_task: deltas + provider-billed reconciliation."""
from __future__ import annotations

from bench import db, report


def _seed(tmp_db) -> None:
    db.record_run({
        "task_id": "lru_ttl_cache", "arm": "local-only",
        "model": "local-long", "input_tok": 4000, "output_tok": 2000,
        "actual_cost": 0.0,    "shadow_cost": 0.042,
        "wall_ms": 25000, "generate_ms": 24800, "ttft_ms": 1100,
        "pytest_passed": 22, "pytest_total": 24,
        "passes_tests": 22 / 24, "composite_score": 0.92,
    })
    db.record_run({
        "task_id": "lru_ttl_cache", "arm": "claude-only",
        "model": "claude-sonnet-4-6",
        "input_tok": 4000, "output_tok": 2000,
        "actual_cost": 0.042, "shadow_cost": 0.042,
        "wall_ms": 9000, "generate_ms": 8800, "ttft_ms": 700,
        "pytest_passed": 24, "pytest_total": 24,
        "passes_tests": 1.0, "composite_score": 0.99,
    })
    db.record_run({
        "task_id": "lru_ttl_cache", "arm": "cursor-no-proxy",
        "model": "claude-sonnet-4-6",
        "input_tok": 6500, "output_tok": 2500,
        "actual_cost": 0.0,    "shadow_cost": 0.057,
        "wall_ms": 12000, "generate_ms": 12000, "ttft_ms": 0,
        "pytest_passed": 23, "pytest_total": 24,
        "passes_tests": 23 / 24, "composite_score": 0.95,
    })


def test_summary_three_arms_basic_shape(tmp_db) -> None:
    _seed(tmp_db)
    rep = report.summarize_task("lru_ttl_cache")
    assert rep["rows"] == 3
    assert set(rep["arms"]) == {"local-only", "claude-only", "cursor-no-proxy"}
    for arm, info in rep["arms"].items():
        assert info["attempts"] == 1
        assert info["mean_score"] > 0.0
        assert info["median_wall_ms"] > 0


def test_provider_billed_overlay_picks_specific_source(tmp_db) -> None:
    _seed(tmp_db)
    runs = db.list_runs(task_id="lru_ttl_cache", arm="claude-only")
    s_ts = runs[0]["ts"]; e_ts = s_ts + 60

    db.record_provider_spend({
        "arm": "claude-only", "task_id": "lru_ttl_cache",
        "window_start": s_ts, "window_end": e_ts,
        "provider": "anthropic", "source": "admin-api-cost",
        "billed_usd": 0.039,
    })

    runs_c = db.list_runs(task_id="lru_ttl_cache", arm="cursor-no-proxy")
    c_ts = runs_c[0]["ts"]; c_end = c_ts + 60
    db.record_provider_spend({
        "arm": "cursor-no-proxy", "task_id": "lru_ttl_cache",
        "window_start": c_ts, "window_end": c_end,
        "provider": "cursor", "source": "usage-events",
        "billed_usd": 0.085,
    })

    rep = report.summarize_task("lru_ttl_cache")
    a_claude = rep["arms"]["claude-only"]
    a_cursor = rep["arms"]["cursor-no-proxy"]
    a_local  = rep["arms"]["local-only"]

    assert a_claude["provider_billed_usd"] == 0.039
    assert a_claude["provider_billed_source"] == "admin-api-cost"
    assert a_cursor["provider_billed_usd"] == 0.085
    assert a_cursor["provider_billed_source"] == "usage-events"

    # Local arm has no provider billing -> reference falls back to actual ($0).
    assert a_local["provider_billed_usd"] == 0.0
    # local-only is cheapest, so it's the baseline.
    assert a_local["delta_vs_cheapest_pct"] in (0.0, None) or a_local["delta_vs_cheapest_pct"] == 0
    # Cursor is the most expensive of the three.
    assert a_cursor["delta_vs_cheapest_usd"] > a_claude["delta_vs_cheapest_usd"]


def test_summary_empty_task_returns_no_arms(tmp_db) -> None:
    rep = report.summarize_task("does_not_exist")
    assert rep["rows"] == 0
    assert rep["arms"] == {}
