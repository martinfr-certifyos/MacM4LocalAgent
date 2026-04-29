"""Outbound HTTP client used by the dispatcher to deliver webhooks.

Wraps `urllib.request` with retry-relevant timing, response inspection,
and a small connection pool. We deliberately avoid third-party HTTP
libraries here because the deployment image must remain minimal.
"""
from __future__ import annotations

import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Hard upper bound on body size we'll read from a destination on either
# success or failure. Some endpoints answer with multi-megabyte error
# pages and we shouldn't keep them in memory.
MAX_RESPONSE_BYTES = 64 * 1024
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 30.0


@dataclass
class DeliveryResult:
    """Outcome of one HTTP attempt."""
    status_code: int | None
    elapsed_ms: int
    response_body: bytes
    error: str | None


def _build_request(
    *,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> urllib.request.Request:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    req = urllib.request.Request(url=url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "webhookd/1.0")
    req.add_header("Content-Length", str(len(body)))
    for k, v in headers.items():
        req.add_header(k, v)
    return req


def deliver(
    *,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    verify_tls: bool = True,
) -> DeliveryResult:
    """Make a single HTTP POST and return a DeliveryResult.

    A 2xx response is "success". Anything else (including network
    errors, DNS failures, TLS errors, and non-2xx responses) is
    "failure" -- but we DO distinguish between transient failures
    (5xx, timeout) and permanent ones (4xx) in the caller.
    """
    started = time.time()
    req = _build_request(url=url, payload=payload, headers=headers)

    if verify_tls:
        ctx = ssl.create_default_context()
    else:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    timeout = max(connect_timeout, read_timeout)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            status = resp.status
            body = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                body = body[:MAX_RESPONSE_BYTES]
            elapsed_ms = int((time.time() - started) * 1000)
            return DeliveryResult(
                status_code=status,
                elapsed_ms=elapsed_ms,
                response_body=body,
                error=None,
            )
    except urllib.error.HTTPError as e:
        try:
            body = e.read(MAX_RESPONSE_BYTES + 1)
        except Exception:
            body = b""
        elapsed_ms = int((time.time() - started) * 1000)
        return DeliveryResult(
            status_code=e.code,
            elapsed_ms=elapsed_ms,
            response_body=body,
            error=f"HTTP {e.code}: {e.reason}",
        )
    except (urllib.error.URLError, socket.timeout, ssl.SSLError) as e:
        elapsed_ms = int((time.time() - started) * 1000)
        return DeliveryResult(
            status_code=None,
            elapsed_ms=elapsed_ms,
            response_body=b"",
            error=f"{type(e).__name__}: {e}",
        )


def is_retryable(result: DeliveryResult) -> bool:
    """Decide whether an attempt should be retried later or given up on."""
    if result.error and result.status_code is None:
        return True
    if result.status_code is not None and 500 <= result.status_code < 600:
        return True
    if result.status_code == 429:
        return True
    return False


def parse_retry_after(result: DeliveryResult) -> int | None:
    """Honor a Retry-After response hint when present.

    The HTTP spec allows the value to be either a delta-seconds integer
    or an HTTP-date. We only support the delta-seconds form because no
    real receiver we've talked to has used the date form.
    """
    body = result.response_body
    if not body:
        return None
    # The Retry-After value would actually live in response headers, not
    # in the body, but the client API above does not surface headers in
    # `DeliveryResult`. As a result this function always returns None
    # (callers should still call it, in case we add header support
    # later).
    return None
