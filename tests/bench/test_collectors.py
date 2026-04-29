"""bench.collectors.{anthropic_admin, cursor_admin}: HTTP wire shape via mocks.

We never hit the real APIs; httpx.MockTransport responds with canned bodies.
"""
from __future__ import annotations

import json

import httpx
import pytest

from bench.collectors import anthropic_admin, cursor_admin


def _mock_anthropic(handler, api_key: str = "sk-ant-admin-test") -> httpx.Client:
    """Build a client with the same headers _client() sets, but a mock transport."""
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=anthropic_admin.API_BASE,
        headers={
            "x-api-key": api_key,
            "anthropic-version": anthropic_admin.ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
    )


def _mock_cursor(handler, api_key: str = "k") -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=cursor_admin.API_BASE,
        auth=(api_key, ""),
        headers={"content-type": "application/json"},
    )


# ---- Anthropic --------------------------------------------------------------

def test_anthropic_usage_normalizes_buckets() -> None:
    body = {
        "data": [
            {
                "starting_at": "2026-04-26T00:00:00Z",
                "ending_at":   "2026-04-27T00:00:00Z",
                "results": [
                    {
                        "model": "claude-sonnet-4-6",
                        "api_key_id": "apikey_abc",
                        "uncached_input_tokens": 1234,
                        "cache_read_input_tokens": 100,
                        "cache_creation_input_tokens": 50,
                        "output_tokens": 567,
                        "service_tier": "standard",
                        "context_window": "0-200k",
                    }
                ],
            }
        ]
    }
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["headers"] = dict(req.headers)
        return httpx.Response(200, json=body)

    client = _mock_anthropic(handler)
    rows = anthropic_admin.fetch_messages_usage(
        window_start=1714000000, window_end=1714003600,
        api_key_ids=["apikey_abc"], api_key="sk-ant-admin-test", client=client,
    )
    client.close()

    assert "/usage_report/messages" in captured["url"]
    assert captured["headers"]["x-api-key"] == "sk-ant-admin-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert len(rows) == 1
    r = rows[0]
    assert r["model"] == "claude-sonnet-4-6"
    assert r["uncached_input_tokens"] == 1234
    assert r["output_tokens"] == 567


def test_anthropic_cost_handles_amount_cents() -> None:
    body = {
        "data": [
            {
                "starting_at": "2026-04-26T00:00:00Z",
                "ending_at":   "2026-04-27T00:00:00Z",
                "results": [{"amount_cents": 1234}],
            },
            {
                "starting_at": "2026-04-27T00:00:00Z",
                "ending_at":   "2026-04-28T00:00:00Z",
                "results": [{"amount": {"value_cents": 2500, "currency": "USD"}}],
            },
        ]
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = _mock_anthropic(handler)
    rows = anthropic_admin.fetch_cost(
        window_start=0, window_end=0, api_key="sk", client=client,
    )
    client.close()

    assert len(rows) == 2
    assert rows[0]["billed_usd"] == 12.34
    assert rows[1]["billed_usd"] == 25.00


def test_anthropic_collect_combines_usage_and_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        anthropic_admin, "fetch_messages_usage",
        lambda **kw: [{
            "model": "claude-sonnet-4-6", "api_key_id": "k1",
            "uncached_input_tokens": 100, "output_tokens": 200,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        }],
    )
    monkeypatch.setattr(
        anthropic_admin, "fetch_cost",
        lambda **kw: [{"billed_usd": 1.23}, {"billed_usd": 4.56}],
    )

    rows = anthropic_admin.collect(
        window_start=0, window_end=1, arm="claude-only", task_id="t",
        api_key="x",
    )
    cost_row = next(r for r in rows if r["source"] == "admin-api-cost")
    assert cost_row["billed_usd"] == pytest.approx(5.79)
    usage_row = next(r for r in rows if r["source"] == "admin-api")
    assert usage_row["input_tok"] == 100
    assert usage_row["output_tok"] == 200


def test_anthropic_admin_error_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_ADMIN_API_KEY", raising=False)
    with pytest.raises(anthropic_admin.AnthropicAdminError):
        anthropic_admin._get_admin_key(env={})


# ---- Cursor -----------------------------------------------------------------

def test_cursor_filtered_usage_events_paginates() -> None:
    pages = [
        {
            "usageEvents": [
                {
                    "timestamp": "1714000000000",
                    "model": "claude-sonnet-4-6",
                    "isTokenBasedCall": True,
                    "tokenUsage": {
                        "inputTokens": 1000, "outputTokens": 500,
                        "cacheReadTokens": 100, "cacheWriteTokens": 50,
                        "totalCents": 250,
                    },
                    "chargedCents": 300,
                },
                {
                    "model": "claude-sonnet-4-6",
                    "tokenUsage": {"inputTokens": 200, "outputTokens": 100,
                                    "cacheReadTokens": 0, "cacheWriteTokens": 0},
                    "chargedCents": 80,
                },
            ],
            "totalUsageEventsCount": 3,
        },
        {
            "usageEvents": [
                {"model": "gpt-5-mini",
                 "tokenUsage": {"inputTokens": 50, "outputTokens": 25,
                                "cacheReadTokens": 0, "cacheWriteTokens": 0},
                 "chargedCents": 10},
            ],
            "totalUsageEventsCount": 3,
        },
    ]
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=pages[calls["n"] - 1])

    client = _mock_cursor(handler)
    events = cursor_admin.fetch_usage_events(
        window_start=1714000000, window_end=1714003600,
        api_key="k", client=client, page_size=2, max_pages=10,
    )
    client.close()
    assert len(events) == 3
    assert calls["n"] == 2


def test_cursor_collect_groups_by_model_and_sums_charged() -> None:
    events = [
        {"model": "claude-sonnet-4-6",
         "tokenUsage": {"inputTokens": 100, "outputTokens": 50,
                        "cacheReadTokens": 0, "cacheWriteTokens": 0},
         "chargedCents": 200},
        {"model": "claude-sonnet-4-6",
         "tokenUsage": {"inputTokens": 50, "outputTokens": 25,
                        "cacheReadTokens": 10, "cacheWriteTokens": 5},
         "chargedCents": 80},
        {"model": "gpt-5-mini",
         "tokenUsage": {"inputTokens": 10, "outputTokens": 5,
                        "cacheReadTokens": 0, "cacheWriteTokens": 0},
         "chargedCents": 5},
    ]

    import bench.collectors.cursor_admin as ca
    import pytest as _pytest
    _orig = ca.fetch_usage_events
    ca.fetch_usage_events = lambda **kw: events  # type: ignore[assignment]
    try:
        rows = ca.collect(
            window_start=0, window_end=1, arm="cursor-no-proxy",
            task_id="t", api_key="k",
        )
    finally:
        ca.fetch_usage_events = _orig  # type: ignore[assignment]

    by_source = {}
    for r in rows:
        by_source.setdefault(r["source"], []).append(r)
    assert "usage-events" in by_source
    assert "usage-events-rollup" in by_source

    # Per-model grouping: 2 rows in 'usage-events' (one per distinct model).
    assert len(by_source["usage-events"]) == 2
    claude_row = next(r for r in by_source["usage-events"]
                      if r["raw_response"]["model"] == "claude-sonnet-4-6")
    assert claude_row["billed_usd"] == _pytest.approx(2.80)
    assert claude_row["input_tok"] == 150
    assert claude_row["output_tok"] == 75

    rollup = by_source["usage-events-rollup"][0]
    assert rollup["billed_usd"] == _pytest.approx(2.85)
    assert rollup["requests"] == 3


def test_cursor_admin_error_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CURSOR_ADMIN_API_KEY", raising=False)
    with pytest.raises(cursor_admin.CursorAdminError):
        cursor_admin._get_key(env={})


def test_cursor_manual_csv_parser(tmp_path) -> None:
    p = tmp_path / "spend.csv"
    p.write_text(
        "timestamp,model,input_tokens,output_tokens,charged_usd\n"
        "2026-04-27T10:00:00Z,claude-sonnet-4-6,1000,500,0.045\n"
        "2026-04-27T10:01:00Z,claude-sonnet-4-6,200,100,0.012\n"
    )
    rows = cursor_admin.parse_manual_spend_csv(
        str(p), arm="cursor-no-proxy", task_id="t",
        window_start=0, window_end=1,
    )
    assert len(rows) == 2
    total = sum(r["billed_usd"] for r in rows)
    assert total == pytest.approx(0.057)
    assert rows[0]["input_tok"] == 1000
    assert rows[0]["output_tok"] == 500
    assert rows[0]["source"] == "manual-csv"
