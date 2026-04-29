"""Validated dataclass models that mirror the storage layer.

The HTTP API marshals JSON dicts into these dataclasses so we can run a
single validation pass at the boundary instead of sprinkling
`isinstance` checks throughout the request handlers. Each class
provides:

    .from_dict(d)       -> validates and constructs
    .to_dict()          -> JSON-serializable view (timestamps as ints)
    .validate()         -> raises `ValidationError` on bad data

Keeping these dataclasses separate from the storage helpers means we
can change the on-disk schema without touching the wire format (and
vice versa). It also gives us a typed surface for unit tests.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any
from urllib.parse import urlparse


_VALID_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*$")
_VALID_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_VALID_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class ValidationError(ValueError):
    """Raised when a model fails validation. The message is safe for
    inclusion in HTTP error responses (no internal details)."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def _check_url(url: str) -> None:
    _require(isinstance(url, str), "url must be a string")
    _require(len(url) <= 2048, "url too long")
    parsed = urlparse(url)
    _require(parsed.scheme in ("http", "https"), "url scheme must be http or https")
    _require(bool(parsed.netloc), "url must include a host")
    # Block private / loopback hosts in production. We don't enforce
    # this in tests because they need to point at 127.0.0.1.
    # (This is documented in security.md.)


@dataclasses.dataclass
class Subscription:
    id: str
    tenant_id: str
    url: str
    secret: str
    event_types: list[str]
    active: bool
    created_at: int
    updated_at: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Subscription":
        return cls(
            id=str(d.get("id", "")),
            tenant_id=str(d.get("tenant_id", "")),
            url=str(d.get("url", "")),
            secret=str(d.get("secret", "")),
            event_types=list(d.get("event_types") or []),
            active=bool(d.get("active", True)),
            created_at=int(d.get("created_at") or 0),
            updated_at=int(d.get("updated_at") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "url": self.url,
            # Secrets are never returned in API responses; callers should
            # use `to_public_dict()` if they're emitting JSON to a tenant.
            "secret": self.secret,
            "event_types": list(self.event_types),
            "active": bool(self.active),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_public_dict(self) -> dict[str, Any]:
        d = self.to_dict()
        d.pop("secret", None)
        return d

    def validate(self) -> None:
        _require(
            bool(_VALID_UUID_RE.match(self.id or "")),
            "subscription id must be a uuid",
        )
        _require(
            bool(_VALID_TENANT_ID_RE.match(self.tenant_id or "")),
            "tenant_id contains invalid characters",
        )
        _check_url(self.url)
        _require(len(self.secret) >= 16, "secret must be at least 16 chars")
        _require(self.event_types, "at least one event_type is required")
        for et in self.event_types:
            _require(
                bool(_VALID_EVENT_TYPE_RE.match(et)),
                f"invalid event_type {et!r}",
            )
        _require(self.created_at > 0, "created_at must be set")
        _require(
            self.updated_at >= self.created_at,
            "updated_at must be >= created_at",
        )


@dataclasses.dataclass
class Event:
    """An incoming event before fan-out."""
    event_id: str
    event_type: str
    payload: dict[str, Any]
    received_at: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        return cls(
            event_id=str(d.get("event_id", "")),
            event_type=str(d.get("event_type", "")),
            payload=dict(d.get("payload") or {}),
            received_at=int(d.get("received_at") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "received_at": self.received_at,
        }

    def validate(self) -> None:
        _require(bool(self.event_id), "event_id is required")
        _require(len(self.event_id) <= 128, "event_id too long")
        _require(
            bool(_VALID_EVENT_TYPE_RE.match(self.event_type)),
            f"invalid event_type {self.event_type!r}",
        )
        _require(isinstance(self.payload, dict), "payload must be an object")


@dataclasses.dataclass
class Delivery:
    """A queued or completed webhook delivery."""
    id: str
    subscription_id: str
    event_id: str
    event_type: str
    payload: dict[str, Any]
    attempts: int
    last_status: int | None
    last_error: str | None
    scheduled_at: int
    delivered_at: int | None
    created_at: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Delivery":
        return cls(
            id=str(d.get("id", "")),
            subscription_id=str(d.get("subscription_id", "")),
            event_id=str(d.get("event_id", "")),
            event_type=str(d.get("event_type", "")),
            payload=dict(d.get("payload") or {}),
            attempts=int(d.get("attempts") or 0),
            last_status=d.get("last_status"),
            last_error=d.get("last_error"),
            scheduled_at=int(d.get("scheduled_at") or 0),
            delivered_at=d.get("delivered_at"),
            created_at=int(d.get("created_at") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subscription_id": self.subscription_id,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "attempts": self.attempts,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "scheduled_at": self.scheduled_at,
            "delivered_at": self.delivered_at,
            "created_at": self.created_at,
        }

    def is_delivered(self) -> bool:
        return self.delivered_at is not None

    def validate(self) -> None:
        _require(bool(_VALID_UUID_RE.match(self.id or "")), "delivery id must be a uuid")
        _require(
            bool(_VALID_UUID_RE.match(self.subscription_id or "")),
            "subscription_id must be a uuid",
        )
        _require(self.attempts >= 0, "attempts must be non-negative")
        _require(self.scheduled_at > 0, "scheduled_at must be set")
        _require(self.created_at > 0, "created_at must be set")
        if self.delivered_at is not None:
            _require(
                self.delivered_at >= self.created_at,
                "delivered_at must be >= created_at",
            )
        if self.last_status is not None:
            _require(
                100 <= self.last_status < 600,
                f"invalid last_status {self.last_status}",
            )


@dataclasses.dataclass
class DeadLetterEntry:
    id: str
    delivery_id: str
    reason: str
    moved_at: int
    original_payload: dict[str, Any]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DeadLetterEntry":
        payload = d.get("original_payload") or {}
        if isinstance(payload, str):
            import json
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        return cls(
            id=str(d.get("id", "")),
            delivery_id=str(d.get("delivery_id", "")),
            reason=str(d.get("reason", "")),
            moved_at=int(d.get("moved_at") or 0),
            original_payload=dict(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "delivery_id": self.delivery_id,
            "reason": self.reason,
            "moved_at": self.moved_at,
            "original_payload": dict(self.original_payload),
        }

    def validate(self) -> None:
        _require(bool(_VALID_UUID_RE.match(self.id or "")), "dlq id must be a uuid")
        _require(
            bool(_VALID_UUID_RE.match(self.delivery_id or "")),
            "delivery_id must be a uuid",
        )
        _require(bool(self.reason), "reason is required")
        _require(self.moved_at > 0, "moved_at must be set")
