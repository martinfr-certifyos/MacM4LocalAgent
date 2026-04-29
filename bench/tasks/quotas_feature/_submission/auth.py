"""Bearer-token authentication for the admin HTTP API.

Each tenant gets a 32-byte random token at signup. We store the SHA-256
hash of the token in the database; the raw token is shown to the tenant
exactly once and never logged. On each request we look up the hashed
token and load the associated tenant id.

Tokens are passed in an `Authorization: Bearer <token>` header.
"""
from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class AuthResult:
    """Outcome of an authentication attempt."""
    ok: bool
    tenant_id: str
    reason: str


# In-memory token table for the demo. A real deployment would back this
# with the same SQLite database.
_TOKEN_TABLE: dict[str, str] = {}


def register_tenant(tenant_id: str) -> str:
    """Generate a fresh token for a tenant. Returns the raw token (only
    chance to see it). The hashed value goes into the in-memory table."""
    raw = os.urandom(32).hex()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    _TOKEN_TABLE[digest] = tenant_id
    return raw


def revoke_tenant(tenant_id: str) -> int:
    """Drop all tokens for a tenant. Returns how many were removed."""
    to_remove = [d for d, tid in _TOKEN_TABLE.items() if tid == tenant_id]
    for d in to_remove:
        del _TOKEN_TABLE[d]
    return len(to_remove)


def _parse_authorization(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.split(None, 1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != "bearer":
        return None
    return token.strip()


def authenticate_request(environ: dict[str, Any]) -> AuthResult:
    """Inspect the WSGI environ and return an AuthResult."""
    header = environ.get("HTTP_AUTHORIZATION")
    token = _parse_authorization(header)
    if token is None:
        return AuthResult(ok=False, tenant_id="", reason="missing_token")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    tenant_id = _TOKEN_TABLE.get(digest)
    if tenant_id is None:
        log.warning("auth: unknown token (digest prefix %s...)", digest[:8])
        return AuthResult(ok=False, tenant_id="", reason="unknown_token")
    return AuthResult(ok=True, tenant_id=tenant_id, reason="")


def authenticate_for_tenant(
    environ: dict[str, Any], expected_tenant_id: str,
) -> AuthResult:
    """Convenience: authenticate AND check the resolved tenant matches."""
    result = authenticate_request(environ)
    if not result.ok:
        return result
    if result.tenant_id == expected_tenant_id:
        return result
    return AuthResult(
        ok=False, tenant_id=result.tenant_id, reason="tenant_mismatch",
    )
