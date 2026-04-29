"""HTTP API for managing subscriptions and inspecting deliveries.

This is a tiny WSGI app (no framework) so the deployment artifact stays
small. It exposes:

    POST   /api/v1/subscriptions
    GET    /api/v1/subscriptions/{id}
    GET    /api/v1/tenants/{tenant_id}/subscriptions
    DELETE /api/v1/subscriptions/{id}
    POST   /api/v1/events                     -- ingest event, fan out
    GET    /api/v1/deliveries/{id}            -- inspect outcome

Authentication is via a per-tenant bearer token validated by
`auth.authenticate_request`. Tenants only ever see their own data.
"""
from __future__ import annotations

import json
import logging
import re
from http import HTTPStatus
from typing import Any, Callable
from urllib.parse import parse_qs

import auth
import dispatcher
import storage

log = logging.getLogger(__name__)

# Routes are defined as (method, regex, handler-name) triples.
_ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    ("POST",   re.compile(r"^/api/v1/subscriptions/?$"), "create_subscription"),
    ("GET",    re.compile(r"^/api/v1/subscriptions/(?P<sid>[0-9a-f-]+)/?$"),
        "get_subscription"),
    ("DELETE", re.compile(r"^/api/v1/subscriptions/(?P<sid>[0-9a-f-]+)/?$"),
        "delete_subscription"),
    ("GET",    re.compile(r"^/api/v1/tenants/(?P<tid>[A-Za-z0-9-]+)/subscriptions/?$"),
        "list_subscriptions"),
    ("POST",   re.compile(r"^/api/v1/events/?$"), "ingest_event"),
    ("GET",    re.compile(r"^/api/v1/deliveries/(?P<did>[0-9a-f-]+)/?$"),
        "get_delivery"),
]


def _json_response(
    start_response: Callable[..., Any],
    status: HTTPStatus,
    body: dict[str, Any] | list[Any],
) -> list[bytes]:
    payload = json.dumps(body).encode("utf-8")
    start_response(
        f"{status.value} {status.phrase}",
        [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(payload))),
        ],
    )
    return [payload]


def _read_body(environ: dict[str, Any]) -> dict[str, Any]:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    if length <= 0:
        return {}
    raw = environ["wsgi.input"].read(length)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


# ---- handlers --------------------------------------------------------------

def create_subscription(
    environ: dict[str, Any],
    start_response: Callable[..., Any],
    *,
    tenant_id: str,
    conn: Any,
    **_: Any,
) -> list[bytes]:
    body = _read_body(environ)
    url = body.get("url")
    secret = body.get("secret")
    event_types = body.get("event_types") or []
    if not url or not secret or not event_types:
        return _json_response(
            start_response, HTTPStatus.BAD_REQUEST,
            {"error": "url, secret, and event_types are required"},
        )
    sub = storage.create_subscription(
        conn,
        tenant_id=tenant_id,
        url=url,
        secret=secret,
        event_types=list(event_types),
    )
    return _json_response(start_response, HTTPStatus.CREATED, sub)


def get_subscription(
    environ: dict[str, Any],
    start_response: Callable[..., Any],
    *,
    tenant_id: str,
    conn: Any,
    sid: str,
    **_: Any,
) -> list[bytes]:
    try:
        sub = storage.get_subscription(conn, sid)
    except KeyError:
        return _json_response(
            start_response, HTTPStatus.NOT_FOUND,
            {"error": f"subscription {sid} not found"},
        )
    if sub["tenant_id"] != tenant_id:
        return _json_response(
            start_response, HTTPStatus.FORBIDDEN,
            {"error": "tenant mismatch"},
        )
    return _json_response(start_response, HTTPStatus.OK, sub)


def delete_subscription(
    environ: dict[str, Any],
    start_response: Callable[..., Any],
    *,
    tenant_id: str,
    conn: Any,
    sid: str,
    **_: Any,
) -> list[bytes]:
    storage.deactivate_subscription(conn, sid)
    return _json_response(start_response, HTTPStatus.NO_CONTENT, {})


def list_subscriptions(
    environ: dict[str, Any],
    start_response: Callable[..., Any],
    *,
    tenant_id: str,
    conn: Any,
    tid: str,
    **_: Any,
) -> list[bytes]:
    if tid != tenant_id:
        return _json_response(
            start_response, HTTPStatus.FORBIDDEN,
            {"error": "tenant mismatch"},
        )
    subs = storage.list_subscriptions_for_tenant(conn, tenant_id)
    return _json_response(start_response, HTTPStatus.OK, subs)


def ingest_event(
    environ: dict[str, Any],
    start_response: Callable[..., Any],
    *,
    tenant_id: str,
    conn: Any,
    **_: Any,
) -> list[bytes]:
    body = _read_body(environ)
    event_id = body.get("event_id")
    event_type = body.get("event_type")
    payload = body.get("payload") or {}
    if not event_id or not event_type:
        return _json_response(
            start_response, HTTPStatus.BAD_REQUEST,
            {"error": "event_id and event_type are required"},
        )
    subs = storage.list_subscriptions_for_tenant(conn, tenant_id)
    delivery_ids: list[str] = []
    for sub in subs:
        if not sub["active"]:
            continue
        if sub["event_types"] and event_type not in sub["event_types"]:
            continue
        did = storage.enqueue_delivery(
            conn,
            subscription_id=sub["id"],
            event_id=event_id,
            event_type=event_type,
            payload=payload,
        )
        delivery_ids.append(did)
    return _json_response(
        start_response, HTTPStatus.ACCEPTED,
        {"event_id": event_id, "delivery_ids": delivery_ids},
    )


def get_delivery(
    environ: dict[str, Any],
    start_response: Callable[..., Any],
    *,
    tenant_id: str,
    conn: Any,
    did: str,
    **_: Any,
) -> list[bytes]:
    row = conn.execute(
        "SELECT * FROM deliveries WHERE id = ?", (did,),
    ).fetchone()
    if row is None:
        return _json_response(
            start_response, HTTPStatus.NOT_FOUND,
            {"error": f"delivery {did} not found"},
        )
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    return _json_response(start_response, HTTPStatus.OK, d)


_HANDLERS = {
    "create_subscription": create_subscription,
    "get_subscription":    get_subscription,
    "delete_subscription": delete_subscription,
    "list_subscriptions":  list_subscriptions,
    "ingest_event":        ingest_event,
    "get_delivery":        get_delivery,
}


def application(
    environ: dict[str, Any],
    start_response: Callable[..., Any],
) -> list[bytes]:
    """WSGI entry point."""
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")

    # Authenticate first so 401s are consistent across all routes.
    auth_result = auth.authenticate_request(environ)
    if not auth_result.ok:
        return _json_response(
            start_response, HTTPStatus.UNAUTHORIZED,
            {"error": auth_result.reason},
        )
    tenant_id = auth_result.tenant_id

    for route_method, regex, handler_name in _ROUTES:
        if method != route_method:
            continue
        m = regex.match(path)
        if m is None:
            continue
        handler = _HANDLERS[handler_name]
        conn = storage.connect()
        try:
            return handler(
                environ, start_response,
                tenant_id=tenant_id,
                conn=conn,
                **m.groupdict(),
            )
        finally:
            conn.close()

    return _json_response(
        start_response, HTTPStatus.NOT_FOUND,
        {"error": f"{method} {path} not found"},
    )
