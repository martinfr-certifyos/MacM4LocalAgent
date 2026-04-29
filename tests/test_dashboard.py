"""Integration tests for the FastAPI dashboard."""

from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from compare import ab
from cost import ingest
from dashboard import app as dash_app


@pytest.fixture
def client(tmp_db) -> TestClient:                                              # noqa: ARG001
    return TestClient(dash_app.app)


def _seed(now: int) -> None:
    ingest.record_request(model="local-fast",        tier="local-fast",  in_tok=1000, out_tok=500, actual_cost=0.0,    latency_ms=100, ts=now-100, route_reason="<= 16k")
    ingest.record_request(model="ollama/qwen3-30b",  tier="local-long",  in_tok=2000, out_tok=800, actual_cost=0.0,    latency_ms=400, ts=now-200, route_reason="16k-128k")
    ingest.record_request(model="claude-sonnet-4-6", tier="claude",      in_tok=500,  out_tok=200, actual_cost=0.0045, latency_ms=900, ts=now-300, route_reason="complex")


def test_index_renders(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "Cost" in r.text
    assert "stats" in r.text   # the htmx target id


def test_stats_fragment(client: TestClient) -> None:
    _seed(int(time.time()))
    r = client.get("/stats")
    assert r.status_code == 200
    assert "Today"        in r.text
    assert "Last 7 days"  in r.text
    assert "claude"       in r.text
    assert "local-fast"   in r.text
    # request rows
    assert "ollama/qwen3-30b" in r.text


def test_api_stats_json(client: TestClient) -> None:
    _seed(int(time.time()))
    r = client.get("/api/stats")
    assert r.status_code == 200
    body = r.json()
    for window in ("today", "week", "month", "all"):
        assert window in body
    assert body["week"]["total_requests"] == 3


def test_compare_index_empty(client: TestClient) -> None:
    r = client.get("/compare")
    assert r.status_code == 200
    assert "No comparisons yet" in r.text


def test_compare_one_404(client: TestClient) -> None:
    r = client.get("/compare/9999")
    assert r.status_code == 404


def _resp(content: str, in_tok: int = 1, out_tok: int = 1) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": in_tok, "completion_tokens": out_tok},
    })


def test_compare_run_creates_row(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        return _resp(f"answer for {body['model']}", 5, 7)

    real_client = httpx.Client

    class _MockClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _MockClient)

    r = client.post("/compare/run", data={"prompt": "say hi"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    location = r.headers["location"]
    assert location.startswith("/compare/")

    one = client.get(location)
    assert one.status_code == 200
    assert "say hi" in one.text
    assert "answer for local-long"  in one.text
    assert "answer for claude-code" in one.text
