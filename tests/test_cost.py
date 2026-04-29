"""Unit tests for the cost SQLite store and the savings CLI."""

from __future__ import annotations

import io
import json
import sqlite3
import time
from contextlib import redirect_stdout

import pytest

from cost import ingest, savings


# ---- shadow_cost math ---------------------------------------------------------

@pytest.mark.parametrize(
    "in_tok,out_tok,expected",
    [
        (0,    0,    0.0),
        (1000, 0,    0.003),                            # 1000 * 3 / 1M
        (0,    1000, 0.015),                            # 1000 * 15 / 1M
        (1_000_000, 1_000_000, 18.0),                   # 3 + 15
        (123, 456, 123 * 3e-6 + 456 * 15e-6),
    ],
)
def test_shadow_cost(in_tok: int, out_tok: int, expected: float) -> None:
    assert ingest.shadow_cost(in_tok, out_tok) == pytest.approx(expected, rel=1e-9)


# ---- schema + connection ------------------------------------------------------

def test_connect_creates_schema(tmp_db) -> None:
    c = ingest.connect()
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "requests" in tables
    assert "comparisons" in tables
    cols = {r[1] for r in c.execute("PRAGMA table_info(requests)")}
    for required in {
        "id", "ts", "model", "tier", "input_tok", "output_tok",
        "actual_cost", "shadow_cost", "latency_ms", "route_reason",
    }:
        assert required in cols


def test_connect_is_idempotent(tmp_db) -> None:
    ingest.connect().close()
    ingest.connect().close()      # second call should not error
    c = ingest.connect()
    rows = c.execute("SELECT COUNT(*) FROM requests").fetchone()
    assert rows[0] == 0


# ---- record_request -----------------------------------------------------------

def test_record_request_inserts(tmp_db) -> None:
    rid = ingest.record_request(
        model="local-fast", tier="local-fast",
        in_tok=100, out_tok=50, actual_cost=0.0,
        latency_ms=120, route_reason="self-test",
    )
    assert rid >= 1
    c = ingest.connect()
    r = c.execute("SELECT model, tier, input_tok, output_tok, actual_cost, shadow_cost FROM requests").fetchone()
    assert r["model"] == "local-fast"
    assert r["tier"]  == "local-fast"
    assert r["input_tok"]  == 100
    assert r["output_tok"] == 50
    assert r["actual_cost"] == 0.0
    assert r["shadow_cost"] == pytest.approx(100 * 3e-6 + 50 * 15e-6, rel=1e-9)


def test_record_request_uses_provided_ts(tmp_db) -> None:
    fixed_ts = 1_700_000_000
    ingest.record_request(
        model="claude-code", tier="claude",
        in_tok=1, out_tok=1, actual_cost=0.001,
        ts=fixed_ts,
    )
    c = ingest.connect()
    ts = c.execute("SELECT ts FROM requests").fetchone()["ts"]
    assert ts == fixed_ts


# ---- record_comparison --------------------------------------------------------

def test_record_comparison_inserts(tmp_db) -> None:
    rid = ingest.record_comparison({
        "prompt": "say hi",
        "local_model": "local-long",
        "claude_model": "claude-code",
        "local_output": "hi",
        "claude_output": "hello",
        "local_in_tok":  10, "local_out_tok":  3,
        "claude_in_tok": 10, "claude_out_tok": 4,
        "local_cost":  0.0,
        "claude_cost": 0.000_09,
        "local_ms": 200, "claude_ms": 800,
        "judge_score": 0.92,
    })
    assert rid >= 1
    c = ingest.connect()
    r = c.execute("SELECT * FROM comparisons").fetchone()
    assert r["judge_score"] == pytest.approx(0.92)
    assert r["claude_ms"] == 800


# ---- savings.summarize --------------------------------------------------------

def _seed(now: int) -> None:
    ingest.record_request(model="local-fast",        tier="local-fast",  in_tok=1000, out_tok=500, actual_cost=0.0,    latency_ms=100, ts=now-3600)
    ingest.record_request(model="ollama/qwen3-30b",  tier="local-long",  in_tok=2000, out_tok=800, actual_cost=0.0,    latency_ms=400, ts=now-7200)
    ingest.record_request(model="claude-sonnet-4-6", tier="claude",      in_tok=500,  out_tok=200, actual_cost=0.0045, latency_ms=900, ts=now-1800)


def test_summarize_7d(tmp_db) -> None:
    now = int(time.time())
    _seed(now)
    s = savings.summarize(7)
    assert s["total_requests"] == 3
    assert s["total_input_tokens"]  == 3500
    assert s["total_output_tokens"] == 1500
    assert s["actual_spend_usd"]    == pytest.approx(0.0045, rel=1e-6)
    assert s["shadow_spend_usd"]    > s["actual_spend_usd"]
    assert s["savings_usd"]         > 0
    assert {"local-fast", "local-long", "claude"}.issubset(s["by_tier"].keys())
    assert s["by_tier"]["claude"]["actual_usd"] == pytest.approx(0.0045, rel=1e-6)


def test_summarize_window_excludes_old(tmp_db) -> None:
    now = int(time.time())
    # Far in the past:
    ingest.record_request(model="claude-code", tier="claude", in_tok=1, out_tok=1, actual_cost=1.0, ts=now - 86400 * 60)
    s7 = savings.summarize(7)
    assert s7["total_requests"] == 0
    s_all = savings.summarize(None)
    assert s_all["total_requests"] == 1


def test_summarize_empty(tmp_db) -> None:
    s = savings.summarize(7)
    assert s["total_requests"] == 0
    assert s["actual_spend_usd"] == 0.0
    assert s["shadow_spend_usd"] == 0.0
    assert s["savings_usd"] == 0.0
    assert s["savings_pct"] == 0.0


# ---- savings.main / CLI -------------------------------------------------------

def test_savings_main_json(tmp_db, capsys) -> None:
    _seed(int(time.time()))
    rc = savings.main(["--json", "7"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["total_requests"] == 3


def test_savings_main_human(tmp_db, capsys) -> None:
    _seed(int(time.time()))
    rc = savings.main(["7"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Last 7 days" in out
    assert "Savings" in out


def test_savings_main_no_args_three_blocks(tmp_db, capsys) -> None:
    _seed(int(time.time()))
    rc = savings.main([])
    assert rc == 0
    out = capsys.readouterr().out
    for header in ("Today", "Last 7 days", "Last 30 days", "All time"):
        assert header in out
