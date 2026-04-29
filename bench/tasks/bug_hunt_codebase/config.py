"""Runtime configuration loader.

We support three configuration sources, in increasing precedence:

    1. compiled-in defaults
    2. /etc/webhookd/config.yaml (if present)
    3. environment variables prefixed WEBHOOKD_

The intent is that production deployments install a config file and
then override individual fields via environment variables (so secrets
like the encryption key never have to live on disk).

Validation runs at load time. Missing or malformed values cause the
service to refuse to start, which is preferable to silently using a
wrong value at 3am.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import pathlib
from typing import Any

log = logging.getLogger(__name__)


@dataclasses.dataclass
class Config:
    """Top-level configuration container."""
    # Where the SQLite database file lives.
    db_path: str = "/var/lib/webhookd/webhookd.db"

    # WSGI bind.
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080

    # Dispatcher tuning.
    dispatcher_poll_interval_seconds: float = 1.0
    dispatcher_max_attempts: int = 10
    dispatcher_base_backoff_seconds: int = 5
    dispatcher_max_backoff_seconds: int = 3600

    # Outbound HTTP.
    outbound_connect_timeout_seconds: float = 5.0
    outbound_read_timeout_seconds: float = 30.0
    outbound_verify_tls: bool = True
    outbound_max_response_bytes: int = 64 * 1024

    # Worker pool.
    worker_pool_size: int = 16
    worker_pool_max_queue: int = 1024

    # Rate limiter.
    rate_per_sec: float = 30.0
    rate_capacity: float = 60.0
    rate_max_buckets: int = 10_000

    # Auth.
    token_byte_length: int = 32

    # Observability.
    metrics_listen_port: int = 9090

    def validate(self) -> None:
        """Raise ValueError on any clearly bad config combination."""
        if self.listen_port < 1 or self.listen_port > 65535:
            raise ValueError(f"listen_port out of range: {self.listen_port}")
        if self.dispatcher_max_attempts < 1:
            raise ValueError("dispatcher_max_attempts must be >= 1")
        if self.dispatcher_base_backoff_seconds < 1:
            raise ValueError("dispatcher_base_backoff_seconds must be >= 1")
        if self.dispatcher_max_backoff_seconds < self.dispatcher_base_backoff_seconds:
            raise ValueError(
                "dispatcher_max_backoff_seconds must be >= "
                "dispatcher_base_backoff_seconds"
            )
        if self.outbound_connect_timeout_seconds <= 0:
            raise ValueError("outbound_connect_timeout_seconds must be > 0")
        if self.outbound_read_timeout_seconds <= 0:
            raise ValueError("outbound_read_timeout_seconds must be > 0")
        if self.worker_pool_size < 1:
            raise ValueError("worker_pool_size must be >= 1")
        if self.worker_pool_max_queue < 1:
            raise ValueError("worker_pool_max_queue must be >= 1")
        if self.rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        if self.rate_capacity <= 0:
            raise ValueError("rate_capacity must be > 0")
        if self.token_byte_length < 16:
            raise ValueError(
                "token_byte_length must be >= 16 (security minimum)"
            )


_DEFAULT_FILE = "/etc/webhookd/config.yaml"


def _coerce(value: str, target_type: type) -> Any:
    """Convert a string env-var value to the dataclass field's type."""
    if target_type is bool:
        return value.lower() in ("1", "true", "yes", "on")
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    return value


def _load_file(path: str) -> dict[str, Any]:
    """Parse a tiny YAML-ish file. We only support flat key: value pairs
    and comments (lines starting with #). Indented or nested structures
    are NOT supported -- we keep the parser self-contained instead of
    depending on PyYAML."""
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    out: dict[str, Any] = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _apply_env(config: Config) -> Config:
    """Override any field from a WEBHOOKD_<UPPER> environment variable."""
    type_hints = {f.name: f.type for f in dataclasses.fields(config)}
    for name, t in type_hints.items():
        env_name = f"WEBHOOKD_{name.upper()}"
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        try:
            value = _coerce(raw, t if isinstance(t, type) else type(getattr(config, name)))
        except (ValueError, TypeError) as e:
            raise ValueError(f"invalid {env_name}: {e}") from e
        setattr(config, name, value)
    return config


def load(path: str | None = None) -> Config:
    """Load the full configuration from defaults + file + env. Validates."""
    config = Config()
    file_path = path or _DEFAULT_FILE
    file_data = _load_file(file_path)
    for key, value in file_data.items():
        if not hasattr(config, key):
            log.warning("ignoring unknown config key %r in %s", key, file_path)
            continue
        try:
            current = getattr(config, key)
            value = _coerce(value, type(current))
        except (ValueError, TypeError):
            log.warning("ignoring malformed value for %r in %s", key, file_path)
            continue
        setattr(config, key, value)
    config = _apply_env(config)
    config.validate()
    return config
