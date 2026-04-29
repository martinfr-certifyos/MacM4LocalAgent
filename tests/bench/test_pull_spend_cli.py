"""bench.pull_spend CLI: graceful skips when keys are missing, dry-run path,
and DB writes when collectors return rows."""
from __future__ import annotations

import pytest

from bench import db, pull_spend


def test_skips_anthropic_without_key(tmp_db, monkeypatch: pytest.MonkeyPatch,
                                     capsys: pytest.CaptureFixture) -> None:
    monkeypatch.delenv("ANTHROPIC_ADMIN_API_KEY", raising=False)
    monkeypatch.delenv("CURSOR_ADMIN_API_KEY",    raising=False)
    rc = pull_spend.main([
        "--arm", "claude-only",
        "--task-id", "lru_ttl_cache",
        "--window-start", "1000",
        "--window-end",   "2000",
        "--providers", "anthropic",
    ])
    out = capsys.readouterr().err
    assert rc == 2  # at least one error -> exit 2
    assert "ANTHROPIC_ADMIN_API_KEY" in out


def test_writes_rows_when_collectors_succeed(tmp_db,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    from bench.collectors import anthropic_admin, cursor_admin
    monkeypatch.setattr(anthropic_admin, "collect", lambda **kw: [
        {"arm": kw["arm"], "task_id": kw["task_id"],
         "window_start": kw["window_start"], "window_end": kw["window_end"],
         "provider": "anthropic", "source": "admin-api-cost",
         "billed_usd": 1.23},
    ])
    monkeypatch.setattr(cursor_admin, "collect", lambda **kw: [
        {"arm": kw["arm"], "task_id": kw["task_id"],
         "window_start": kw["window_start"], "window_end": kw["window_end"],
         "provider": "cursor", "source": "usage-events-rollup",
         "billed_usd": 4.56, "requests": 3},
    ])
    monkeypatch.setenv("ANTHROPIC_ADMIN_API_KEY", "sk-ant-admin-x")
    monkeypatch.setenv("CURSOR_ADMIN_API_KEY", "k")
    monkeypatch.delenv("CURSOR_MANUAL_SPEND_CSV", raising=False)

    rc = pull_spend.main([
        "--arm", "claude-only",
        "--task-id", "t",
        "--window-start", "100", "--window-end", "200",
        "--providers", "anthropic,cursor",
    ])
    assert rc == 0
    s = db.provider_spend_for_window(arm="claude-only", window_start=100, window_end=200)
    assert s["total_billed_usd"] == pytest.approx(1.23 + 4.56)


def test_dry_run_does_not_write(tmp_db, monkeypatch: pytest.MonkeyPatch) -> None:
    from bench.collectors import anthropic_admin
    monkeypatch.setattr(anthropic_admin, "collect", lambda **kw: [
        {"arm": kw["arm"], "task_id": kw["task_id"],
         "window_start": kw["window_start"], "window_end": kw["window_end"],
         "provider": "anthropic", "source": "admin-api-cost", "billed_usd": 9.0},
    ])
    monkeypatch.setenv("ANTHROPIC_ADMIN_API_KEY", "sk")
    rc = pull_spend.main([
        "--arm", "claude-only", "--task-id", "t",
        "--window-start", "1", "--window-end", "2",
        "--providers", "anthropic", "--dry-run",
    ])
    assert rc == 0
    s = db.provider_spend_for_window(arm="claude-only", window_start=1, window_end=2)
    assert s["total_billed_usd"] == 0.0
