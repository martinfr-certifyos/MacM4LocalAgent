"""Hidden acceptance tests for the `quotas` feature on the webhookd codebase.

The submission is laid down in `_submission/` next to this test file. The
submission contains the full original codebase plus whatever the model
added/edited. We import everything from there.

All tests run against an in-memory SQLite DB so they are fast and isolated.
The `quotas` module is required: the model can either create a new
`quotas.py` next to the other modules OR re-export the three required
helpers from `storage.py`. We try a sequence of import paths.
"""
from __future__ import annotations

import importlib
import io
import json
import sqlite3
import sys
import time
import types
from pathlib import Path
from typing import Any, Callable, Iterable

import pytest


# ---- module loading --------------------------------------------------------

SUBMISSION_DIR = Path(__file__).parent / "_submission"


def _ensure_on_syspath() -> None:
    p = str(SUBMISSION_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)


def _import(modname: str) -> types.ModuleType:
    """Import a module from the submission. We keep the module cached so
    that importers (e.g. `api` calling `import auth`) and the test code
    see the same module object. The submission directory is always
    placed at the front of sys.path so `import storage` etc. resolve to
    the submitted versions even if the host environment also has them."""
    _ensure_on_syspath()
    return importlib.import_module(modname)


@pytest.fixture
def storage() -> types.ModuleType:
    return _import("storage")


@pytest.fixture
def quotas() -> types.ModuleType:
    """Try `quotas` first, then fall back to using `storage` as the
    quota-functions provider (i.e. the model added them in storage.py)."""
    _ensure_on_syspath()
    for name in ("quotas", "storage"):
        try:
            mod = _import(name)
        except ImportError:
            continue
        if all(hasattr(mod, fn) for fn in ("get_quota", "set_quota", "try_consume_quota")):
            return mod
    pytest.fail(
        "submission must expose get_quota / set_quota / try_consume_quota "
        "either from a `quotas` module or from `storage`"
    )


@pytest.fixture
def conn(storage: types.ModuleType, tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "webhookd.db"
    c = storage.connect(str(db_path))
    yield c
    c.close()


# ---- direct quota helper tests --------------------------------------------

class TestQuotaHelpers:
    def test_get_quota_autocreates_unlimited(self, quotas, conn) -> None:
        q = quotas.get_quota(conn, "tenant-a")
        assert q["monthly_limit"] == 0
        assert q["current_count"] == 0
        assert q["period_start"] > 0
        # 0 means unlimited; remaining is conventionally None or a sentinel
        assert q.get("remaining") in (None, 0, -1, float("inf"))

    def test_set_quota_inserts_and_updates(self, quotas, conn) -> None:
        quotas.set_quota(conn, "tenant-a", monthly_limit=100)
        q1 = quotas.get_quota(conn, "tenant-a")
        assert q1["monthly_limit"] == 100
        assert q1["current_count"] == 0

        quotas.set_quota(conn, "tenant-a", monthly_limit=200)
        q2 = quotas.get_quota(conn, "tenant-a")
        assert q2["monthly_limit"] == 200
        # current_count must be preserved across set_quota updates
        assert q2["current_count"] == 0

    def test_set_quota_preserves_count(self, quotas, conn) -> None:
        quotas.set_quota(conn, "tenant-a", monthly_limit=10)
        # consume some, then change the limit
        for _ in range(3):
            assert quotas.try_consume_quota(conn, "tenant-a") is True
        quotas.set_quota(conn, "tenant-a", monthly_limit=20)
        q = quotas.get_quota(conn, "tenant-a")
        assert q["current_count"] == 3
        assert q["monthly_limit"] == 20

    def test_try_consume_under_limit_returns_true(self, quotas, conn) -> None:
        quotas.set_quota(conn, "tenant-a", monthly_limit=5)
        for i in range(5):
            assert quotas.try_consume_quota(conn, "tenant-a") is True, f"i={i}"
        q = quotas.get_quota(conn, "tenant-a")
        assert q["current_count"] == 5

    def test_try_consume_over_limit_returns_false(self, quotas, conn) -> None:
        quotas.set_quota(conn, "tenant-a", monthly_limit=2)
        assert quotas.try_consume_quota(conn, "tenant-a") is True
        assert quotas.try_consume_quota(conn, "tenant-a") is True
        # Third must be rejected and not increment.
        assert quotas.try_consume_quota(conn, "tenant-a") is False
        q = quotas.get_quota(conn, "tenant-a")
        assert q["current_count"] == 2

    def test_try_consume_zero_means_unlimited(self, quotas, conn) -> None:
        # Default (autocreated) row has monthly_limit=0; should never reject.
        for _ in range(50):
            assert quotas.try_consume_quota(conn, "tenant-a") is True
        q = quotas.get_quota(conn, "tenant-a")
        assert q["current_count"] == 50

    def test_try_consume_n_atomic(self, quotas, conn) -> None:
        """Consuming n at once must be all-or-nothing."""
        quotas.set_quota(conn, "tenant-a", monthly_limit=5)
        # 5 budget, ask for 3 -- ok
        assert quotas.try_consume_quota(conn, "tenant-a", n=3) is True
        # 2 left, ask for 3 -- rejected, count unchanged
        assert quotas.try_consume_quota(conn, "tenant-a", n=3) is False
        q = quotas.get_quota(conn, "tenant-a")
        assert q["current_count"] == 3

    def test_period_rolls_over_after_31_days(self, quotas, conn) -> None:
        quotas.set_quota(conn, "tenant-a", monthly_limit=2)
        assert quotas.try_consume_quota(conn, "tenant-a") is True
        assert quotas.try_consume_quota(conn, "tenant-a") is True
        assert quotas.try_consume_quota(conn, "tenant-a") is False

        # Rewind period_start to >31 days ago.
        rewind = int(time.time()) - 32 * 86400
        conn.execute(
            "UPDATE tenant_quotas SET period_start = ? WHERE tenant_id = ?",
            (rewind, "tenant-a"),
        )
        conn.commit()

        # Next call should roll over and accept again.
        assert quotas.try_consume_quota(conn, "tenant-a") is True
        q = quotas.get_quota(conn, "tenant-a")
        assert q["current_count"] == 1
        assert q["period_start"] >= int(time.time()) - 5

    def test_tenants_isolated(self, quotas, conn) -> None:
        quotas.set_quota(conn, "tenant-a", monthly_limit=1)
        quotas.set_quota(conn, "tenant-b", monthly_limit=1)
        assert quotas.try_consume_quota(conn, "tenant-a") is True
        assert quotas.try_consume_quota(conn, "tenant-a") is False
        # tenant-b unaffected
        assert quotas.try_consume_quota(conn, "tenant-b") is True


# ---- WSGI integration tests ------------------------------------------------

@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Build a callable that mimics WSGI invocation against the submission's
    `api.application`. We patch `storage.connect` to return a single shared
    in-memory connection and authenticate via the `auth` token table."""
    storage_mod = _import("storage")
    api_mod = _import("api")
    auth_mod = _import("auth")

    db_path = tmp_path / "webhookd.db"
    shared_conn = storage_mod.connect(str(db_path))

    class _NonClosingConn:
        """Proxy that forwards everything to the real connection but
        ignores .close() calls -- the WSGI app calls close() in a
        `finally` block after each request, but we want one connection
        to live across all requests in the test."""
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real
        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)
        def close(self) -> None:
            return None

    proxy = _NonClosingConn(shared_conn)
    monkeypatch.setattr(storage_mod, "connect", lambda *a, **kw: proxy)

    tokens = {"tenant-a": "tok_a", "tenant-b": "tok_b"}
    import hashlib
    auth_mod._TOKEN_TABLE.clear()
    for tid, raw in tokens.items():
        digest = hashlib.sha256(raw.encode()).hexdigest()
        auth_mod._TOKEN_TABLE[digest] = tid

    def call(method: str, path: str, *, tenant: str = "tenant-a",
             body: Any = None) -> tuple[int, dict[str, str], Any]:
        body_bytes = b""
        if body is not None:
            body_bytes = json.dumps(body).encode("utf-8")
        environ: dict[str, Any] = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "CONTENT_LENGTH": str(len(body_bytes)),
            "CONTENT_TYPE": "application/json",
            "wsgi.input": io.BytesIO(body_bytes),
            "HTTP_AUTHORIZATION": f"Bearer {tokens[tenant]}",
        }
        captured: dict[str, Any] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            captured["status"] = status
            captured["headers"] = dict(headers)

        result_iter = api_mod.application(environ, start_response)
        result_bytes = b"".join(result_iter)
        try:
            payload = json.loads(result_bytes) if result_bytes else None
        except json.JSONDecodeError:
            payload = result_bytes
        status_code = int(captured["status"].split()[0])
        return status_code, captured["headers"], payload

    return call, shared_conn


def _create_subscription(call: Callable, tenant: str, **fields: Any) -> str:
    body = {
        "url":         "https://example.test/webhook",
        "secret":      "secret_value_at_least_16_chars_long",
        "event_types": ["invoice.created"],
        **fields,
    }
    status, _, payload = call("POST", "/api/v1/subscriptions", tenant=tenant, body=body)
    assert status in (200, 201), f"sub create returned {status} {payload}"
    return payload["id"]


class TestApiQuotaEndpoints:
    def test_get_quota_default(self, app) -> None:
        call, _ = app
        status, _, payload = call("GET", "/api/v1/tenants/tenant-a/quota")
        assert status == 200
        assert payload["monthly_limit"] == 0

    def test_put_quota_sets_and_get_returns(self, app) -> None:
        call, _ = app
        status, _, _ = call("PUT", "/api/v1/tenants/tenant-a/quota",
                            body={"monthly_limit": 50})
        assert status in (200, 204)
        status, _, payload = call("GET", "/api/v1/tenants/tenant-a/quota")
        assert status == 200
        assert payload["monthly_limit"] == 50
        assert payload["current_count"] == 0

    def test_put_quota_rejects_negative(self, app) -> None:
        call, _ = app
        status, _, _ = call("PUT", "/api/v1/tenants/tenant-a/quota",
                            body={"monthly_limit": -1})
        assert status == 400

    def test_get_other_tenant_quota_forbidden(self, app) -> None:
        call, _ = app
        status, _, _ = call("GET", "/api/v1/tenants/tenant-b/quota",
                            tenant="tenant-a")
        assert status == 403

    def test_put_other_tenant_quota_forbidden(self, app) -> None:
        call, _ = app
        status, _, _ = call("PUT", "/api/v1/tenants/tenant-b/quota",
                            tenant="tenant-a", body={"monthly_limit": 10})
        assert status == 403


class TestApiIngestQuotaEnforcement:
    def test_ingest_under_quota_accepted(self, app) -> None:
        call, _ = app
        sid = _create_subscription(call, "tenant-a")
        call("PUT", "/api/v1/tenants/tenant-a/quota",
             body={"monthly_limit": 10})

        status, _, payload = call("POST", "/api/v1/events", body={
            "event_id":   "evt-1",
            "event_type": "invoice.created",
            "payload":    {"amount": 100},
        })
        assert status == 202
        assert len(payload["delivery_ids"]) == 1

    def test_ingest_over_quota_returns_429(self, app) -> None:
        call, _ = app
        sid = _create_subscription(call, "tenant-a")
        call("PUT", "/api/v1/tenants/tenant-a/quota",
             body={"monthly_limit": 1})

        # First event consumes the only quota slot.
        status, _, _ = call("POST", "/api/v1/events", body={
            "event_id":   "evt-1",
            "event_type": "invoice.created",
            "payload":    {},
        })
        assert status == 202

        # Second event must be rejected.
        status, _, payload = call("POST", "/api/v1/events", body={
            "event_id":   "evt-2",
            "event_type": "invoice.created",
            "payload":    {},
        })
        assert status == 429
        assert "quota" in (payload.get("error") or "").lower()

    def test_partial_fanout_when_quota_runs_out_mid_event(self, app) -> None:
        """If a single event has multiple matching subscriptions and the
        quota runs out partway through, the response should include the
        partial delivery_ids and 429 (or include them as a 'enqueued' field
        with 429 status)."""
        call, _ = app
        # Create 3 subscriptions for the same event type.
        for _ in range(3):
            _create_subscription(call, "tenant-a")
        call("PUT", "/api/v1/tenants/tenant-a/quota",
             body={"monthly_limit": 2})

        status, _, payload = call("POST", "/api/v1/events", body={
            "event_id":   "evt-1",
            "event_type": "invoice.created",
            "payload":    {},
        })
        # 429 because the third sub couldn't enqueue.
        assert status == 429
        # But the first two delivery_ids should still be present somewhere.
        ids: Iterable[str] = (
            payload.get("delivery_ids")
            or payload.get("enqueued")
            or []
        )
        assert len(list(ids)) == 2

    def test_unlimited_quota_never_rejects(self, app) -> None:
        call, _ = app
        sid = _create_subscription(call, "tenant-a")
        # Default monthly_limit=0 means unlimited.
        for i in range(20):
            status, _, _ = call("POST", "/api/v1/events", body={
                "event_id":   f"evt-{i}",
                "event_type": "invoice.created",
                "payload":    {},
            })
            assert status == 202, f"i={i}"

    def test_quota_increments_visible_via_get(self, app) -> None:
        call, _ = app
        _create_subscription(call, "tenant-a")
        call("PUT", "/api/v1/tenants/tenant-a/quota",
             body={"monthly_limit": 10})
        for i in range(3):
            call("POST", "/api/v1/events", body={
                "event_id":   f"evt-{i}",
                "event_type": "invoice.created",
                "payload":    {},
            })
        status, _, payload = call("GET", "/api/v1/tenants/tenant-a/quota")
        assert status == 200
        assert payload["current_count"] == 3

    def test_other_tenant_unaffected(self, app) -> None:
        call, _ = app
        _create_subscription(call, "tenant-a")
        _create_subscription(call, "tenant-b")
        call("PUT", "/api/v1/tenants/tenant-a/quota",
             body={"monthly_limit": 1})
        # tenant-a is at 0/1 so first event ok, second 429.
        call("POST", "/api/v1/events", tenant="tenant-a",
             body={"event_id": "a-1", "event_type": "invoice.created", "payload": {}})
        status, _, _ = call("POST", "/api/v1/events", tenant="tenant-a",
            body={"event_id": "a-2", "event_type": "invoice.created", "payload": {}})
        assert status == 429
        # tenant-b has unlimited, so its event must still go through.
        status, _, _ = call("POST", "/api/v1/events", tenant="tenant-b",
            body={"event_id": "b-1", "event_type": "invoice.created", "payload": {}})
        assert status == 202
