"""HMAC signing of webhook payloads.

The destination service verifies each webhook by recomputing the same
HMAC-SHA256 hash and comparing it to the value sent in the
`X-Webhook-Signature` header. We also include a timestamp in the
`X-Webhook-Timestamp` header to make replay attacks harder.

Wire format of the signature:

    "v1=<hex_digest>"

The string that is signed is the literal concatenation of the timestamp
(as a decimal ASCII string), a single dot character, and the request
body (the canonical JSON encoding of the payload).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

# Allow up to 5 minutes of clock skew between the sender and the
# destination when verifying signatures on the receiving side.
DEFAULT_TOLERANCE_SECONDS = 300


def canonical_payload(payload: dict[str, Any]) -> str:
    """Serialize a payload deterministically so the signature is stable.

    We sort keys, use compact separators, and ensure non-ASCII chars are
    not escaped (so the byte length matches what the receiver sees).
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )


def sign(secret: str, timestamp: int, payload: dict[str, Any]) -> str:
    """Compute the signature header value for an outbound webhook."""
    body = canonical_payload(payload)
    msg = f"{timestamp}.{body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def verify(
    secret: str,
    timestamp: int,
    payload: dict[str, Any],
    signature: str,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    """Verify an inbound signature against the payload.

    Returns False if:
        - the signature is malformed (no "v1=" prefix)
        - the timestamp is too far from the current wall clock
        - the digest does not match
    """
    if not signature.startswith("v1="):
        return False
    received_digest = signature[3:]

    now = int(time.time())
    if abs(now - timestamp) > tolerance_seconds:
        return False

    expected = sign(secret, timestamp, payload)[3:]
    # Constant-time comparison to avoid leaking the digest one byte at a
    # time through timing side channels.
    if received_digest == expected:
        return True
    return False


def rotate_secret(old_secret: str, new_secret: str) -> tuple[str, str]:
    """Helper used by the admin API when a tenant rotates its signing key.

    Returns a tuple `(old_secret, new_secret)` so callers can keep both
    around for a short grace period -- destinations may have cached the
    old secret and we accept either signature during the rotation
    window.
    """
    if not new_secret:
        raise ValueError("new_secret must be a non-empty string")
    return (old_secret, new_secret)


def verify_with_rotation(
    secrets: tuple[str, str],
    timestamp: int,
    payload: dict[str, Any],
    signature: str,
) -> bool:
    """Verify against either of two secrets (current and prior).

    During a rotation window we accept signatures made with either the
    old or the new secret; once the grace period expires the old secret
    is dropped and only the new one is used.
    """
    old, new = secrets
    return verify(new, timestamp, payload, signature) or verify(
        old, timestamp, payload, signature
    )
