"""Offline-mode guard for the hybrid local + cloud router.

When the network is unreachable, or the user explicitly sets
`OFFLINE=1` (e.g. on an airplane), the router must never attempt a
Claude call. Instead it transparently downgrades every claude-* model
selection to `local-long`, stamps a `route_reason` flag for the cost
ledger, and emits a one-time warning recommending the user clear
their Cline context -- earlier turns in the same task may have been
shaped by Claude's deeper reasoning and the local model will produce
better output starting from a fresh task.

This module is intentionally standalone:

  - No LiteLLM imports (tests must run without the proxy).
  - No imports from `route_by_size` (it imports us, not the other way
    around -- circular-import safety).
  - The network probe is bounded (1.5s connect timeout), threaded,
    and cached so a routing decision never blocks for more than
    ~1.5s and a flaky link doesn't thrash on every turn.

Decision precedence (highest first):

  1. `OFFLINE=1` in real env       -> offline (skip probe entirely)
  2. `OFFLINE=0` in real env       -> online (skip probe; trust user)
  3. `OFFLINE=auto` in real env    -> probe api.anthropic.com:443
  4. Same keys in `detected.env`   -> same semantics, lower priority
  5. Nothing set                   -> probe (same as auto)

Strict mode (`OFFLINE_STRICT=1`): an explicit Claude request
(direct `claude-*` model name, `gpt-claude-*` Cursor alias, or a
leading `[claude]`/`[opus]`/`[sonnet]`/`[haiku]` tag) raises an
HTTP 503 instead of silently downgrading. Off by default -- silent
downgrade is friendlier for everyday flight-mode use, where the
user just wants the proxy to keep working.
"""

from __future__ import annotations

import os
import pathlib
import socket
import sys
import threading
import time
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Probe target. Anthropic's API endpoint -- the actual destination
# for every claude-* upstream call. If we can't reach this host:port,
# Claude is unreachable regardless of what the user asks for.
_PROBE_HOST = "api.anthropic.com"
_PROBE_PORT = 443
_PROBE_TIMEOUT_SEC = 1.5

# Cache TTLs. Asymmetric on purpose:
#   - Online cache is long-ish so a stable wifi link doesn't trigger
#     a syscall on every routed turn.
#   - Offline cache is short so the moment wifi comes back, the proxy
#     re-detects and the next escalation reaches Claude.
_ONLINE_TTL_SEC = 30.0
_OFFLINE_TTL_SEC = 10.0

# Process-local state, guarded by a lock so async + sync callbacks
# don't race on the cache.
_state_lock = threading.Lock()
_cache: tuple[bool, float] | None = None  # (is_online, last_check_ts)
_warned_session: bool = False  # one-time stderr banner per process

# Where transitions get appended for audit / dashboard ingestion.
# Best-effort: a failure to write here MUST NOT break a request.
_OFFLINE_LOG_PATH = REPO_ROOT / ".logs" / "offline-events.log"


def _read_env_file(path: pathlib.Path) -> dict[str, str]:
    """Tiny duplicate of route_by_size._read_env_file. Kept local so
    this module has no internal imports and stays safe to load from
    test paths where the env file may be absent."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        return {}
    return out


def _env(name: str) -> str | None:
    """Return real env first, then detected.env, else None. We do NOT
    cache the file read -- it's a few dozen lines and reading it
    fresh per resolution lets `make offline` take effect immediately
    without a proxy restart."""
    raw = os.environ.get(name)
    if raw is not None:
        return raw
    file_env = _read_env_file(REPO_ROOT / "config" / "detected.env")
    return file_env.get(name)


def _is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _is_falsy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"0", "false", "no", "off"}


def _probe_anthropic() -> bool:
    """Open a TCP connection to api.anthropic.com:443 with a 1.5s
    timeout. Returns True on success, False on any failure (DNS,
    timeout, refused, no route, etc.). Never raises.

    We deliberately do NOT do an HTTPS handshake here -- a TCP connect
    is enough to distinguish "no network" from "network up". A 200 OK
    from Anthropic would require a valid API key in scope, which this
    module shouldn't touch."""
    sock = None
    try:
        sock = socket.create_connection(
            (_PROBE_HOST, _PROBE_PORT),
            timeout=_PROBE_TIMEOUT_SEC,
        )
        return True
    except Exception:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def _check_online_uncached() -> bool:
    """Resolve OFFLINE/OFFLINE flag precedence then probe if needed.

    Returns True if Claude is reachable, False if offline.

    The forced cases (OFFLINE=1 / OFFLINE=0) skip the probe entirely.
    `OFFLINE=0` is "trust me, I'm online" -- we still skip the probe
    because the user is explicitly disabling offline mode, e.g. for a
    flaky network where the probe would false-positive (some captive
    portals leave port 443 reachable on a sinkhole)."""
    raw = _env("OFFLINE")
    if _is_truthy(raw):
        return False  # forced offline
    if _is_falsy(raw):
        return True   # forced online (skip probe)
    # auto / unset: probe.
    return _probe_anthropic()


def is_online(force_refresh: bool = False) -> bool:
    """Cached, thread-safe online check. Returns True if Claude is
    reachable right now (or believed to be based on a recent probe).

    `force_refresh=True` bypasses the cache. Used by `/offline-status`
    in the dashboard and by `make offline-status` so the operator can
    confirm the proxy will route Claude away if needed."""
    global _cache
    now = time.time()
    with _state_lock:
        if not force_refresh and _cache is not None:
            online, ts = _cache
            ttl = _ONLINE_TTL_SEC if online else _OFFLINE_TTL_SEC
            if (now - ts) < ttl:
                return online
    # Cache miss / forced refresh / expired entry: run the check
    # OUTSIDE the lock so a 1.5s probe doesn't serialize everything.
    online = _check_online_uncached()
    with _state_lock:
        _cache = (online, time.time())
    return online


def is_offline(force_refresh: bool = False) -> bool:
    """Inverse of is_online(). Provided for readability at call
    sites -- `if is_offline(): ...` reads better than the negation."""
    return not is_online(force_refresh=force_refresh)


class OfflineStrictReject(RuntimeError):
    """Raised by maybe_downgrade() when OFFLINE_STRICT=1 and the
    request explicitly asked for Claude. Subclass of RuntimeError
    so it propagates cleanly through LiteLLM's hook surface; the
    `async_pre_call_hook` wrapper in route_by_size.py re-raises
    this specific class rather than swallowing it like normal
    routing errors."""


def offline_reason() -> str:
    """Human-readable reason string for the route_reason log line.

    Returned only after we've decided the request is offline-shaped;
    callers should gate on `is_offline()` first."""
    raw = _env("OFFLINE")
    if _is_truthy(raw):
        return "explicit OFFLINE=1"
    return f"network unreachable (probe {_PROBE_HOST}:{_PROBE_PORT} failed)"


def is_strict() -> bool:
    """Strict mode = explicit Claude requests raise instead of being
    silently downgraded. Off by default."""
    return _is_truthy(_env("OFFLINE_STRICT"))


# --- Model classification (small, no router/route_by_size import) ----

_CLAUDE_PREFIXES = ("claude-", "gpt-claude-")
_CLAUDE_UPSTREAM_PREFIXES = ("anthropic/",)


def is_claude_model(model: str | None) -> bool:
    """True if `model` resolves to a Claude tier on this proxy.

    Matches:
      - top-level aliases: claude-code, claude-opus-4-7, claude-haiku-4-5, ...
      - Cursor-shaped mirrors: gpt-claude-code, gpt-claude-opus-4-7, ...
      - upstream ids:          anthropic/claude-opus-4-7 (post-routing)
    """
    if not model:
        return False
    m = model.lower()
    if m.startswith(_CLAUDE_PREFIXES):
        return True
    if m.startswith(_CLAUDE_UPSTREAM_PREFIXES):
        return True
    # Defensive: bare "claude" anywhere in the upstream id.
    return "claude" in m and m.startswith(("anthropic/", "openai/anthropic/"))


# --- Warning surface --------------------------------------------------

_OFFLINE_NOTICE_TEMPLATE = (
    "[offline-mode] The network is unreachable and Claude is not "
    "available. This turn ran on the local model ({local_model}) "
    "instead of {requested_model}. If your prior conversation "
    "depended on cloud-tier reasoning, consider clearing the Cline "
    "task (Cmd+Shift+P -> 'Cline: New Task') so the local model "
    "doesn't try to extend Claude-shaped context."
)


def render_user_notice(requested_model: str, local_model: str) -> str:
    """The string we surface to the user (via injected system message
    or stderr) when a downgrade happens. Kept here so docs and the
    dashboard can render the same text without copy-pasting."""
    return _OFFLINE_NOTICE_TEMPLATE.format(
        requested_model=requested_model or "claude",
        local_model=local_model,
    )


def _warn_once(message: str) -> None:
    """Print a stderr banner the first time we go offline in this
    process. Subsequent transitions don't re-print -- the operator
    only needs to see it once to understand why their Claude calls
    just got downgraded."""
    global _warned_session
    with _state_lock:
        if _warned_session:
            return
        _warned_session = True
    print(f"[router][offline] {message}", file=sys.stderr, flush=True)


def _log_event(payload: dict[str, Any]) -> None:
    """Append a one-line JSON event to .logs/offline-events.log so the
    dashboard and audits can reconstruct when downgrades fired.
    Best-effort: never raises."""
    try:
        _OFFLINE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        import json
        line = json.dumps({"ts": int(time.time()), **payload})
        with _OFFLINE_LOG_PATH.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# --- Public API used by route_by_size --------------------------------

# Local fallback target. local-long carries the largest context and
# is the only local tier capable of running Cline's harness -- so
# it's the safest universal substitute for a Claude downgrade.
DEFAULT_OFFLINE_FALLBACK = "local-long"


def maybe_downgrade(
    data: dict[str, Any],
    *,
    requested_alias: str,
    explicit_claude: bool,
) -> tuple[bool, str | None]:
    """Mutates `data` in place if the request needs to be downgraded
    because we're offline. Returns (downgraded, error_message).

    - `requested_alias`: what the client asked for BEFORE any routing
      rewrote it (e.g. "hybrid-auto", "claude-code", "gpt-opus", ...).
      Used purely for the warning string + audit log.
    - `explicit_claude`: True when the caller explicitly asked for a
      Claude tier (a direct claude-* model name, a gpt-claude-* alias,
      or a `[claude]`/`[opus]`/`[sonnet]`/`[haiku]` task tag). Drives
      strict-mode behaviour.

    Returns:
      - (False, None) when we stay online or the request wasn't a
        Claude one in the first place.
      - (True,  None) on a successful downgrade. The caller should
        treat this as "we already wrote the new model + metadata to
        `data`, keep going".
      - (True,  msg)  in strict mode: `msg` is a user-facing error
        message the caller should turn into an HTTP 503. We still
        mark `data["metadata"]["offline_downgrade"] = True` so the
        cost ledger can see the attempted-but-rejected request.
    """
    current_model = data.get("model", "")
    if not is_claude_model(current_model):
        return (False, None)
    if not is_offline():
        return (False, None)

    reason = offline_reason()

    if explicit_claude and is_strict():
        meta = data.setdefault("metadata", {})
        meta["offline_downgrade"] = "rejected-strict"
        meta["offline_reason"] = reason
        meta["offline_orig_model"] = requested_alias or current_model
        _log_event({
            "kind": "rejected",
            "requested_alias": requested_alias,
            "resolved_model": current_model,
            "reason": reason,
        })
        msg = (
            f"Offline mode is active ({reason}) and OFFLINE_STRICT=1. "
            f"Refusing to attempt a Claude call. Drop OFFLINE_STRICT or "
            f"retry with a [local] tag / local-long model."
        )
        _warn_once(msg)
        # Caller distinguishes (True, msg) -> strict-reject from
        # (True, None) -> silent downgrade. We DO NOT raise here so
        # the caller can decide whether to convert to an HTTP error
        # or attach a 503-shaped payload (see route_by_size.py).
        return (True, msg)

    # Silent downgrade.
    data["model"] = DEFAULT_OFFLINE_FALLBACK
    meta = data.setdefault("metadata", {})
    meta["offline_downgrade"] = True
    meta["offline_reason"] = reason
    meta["offline_orig_model"] = requested_alias or current_model
    meta["offline_user_notice"] = render_user_notice(
        requested_model=requested_alias or current_model,
        local_model=DEFAULT_OFFLINE_FALLBACK,
    )
    # Make the warning visible in the dashboard's route_reason column.
    # We prepend rather than overwrite so existing reasons (e.g.
    # cline-mode escalations) survive.
    existing_reason = meta.get("route_reason", "")
    offline_reason_tag = f"offline-downgrade: {reason}"
    meta["route_reason"] = (
        f"{offline_reason_tag}; orig={requested_alias or current_model}; "
        f"prev={existing_reason}" if existing_reason else
        f"{offline_reason_tag}; orig={requested_alias or current_model}"
    )

    _log_event({
        "kind": "downgrade",
        "requested_alias": requested_alias,
        "resolved_model": current_model,
        "fallback": DEFAULT_OFFLINE_FALLBACK,
        "reason": reason,
        "explicit_claude": explicit_claude,
    })
    _warn_once(
        f"network unreachable ({reason}); downgrading "
        f"{requested_alias or current_model} -> {DEFAULT_OFFLINE_FALLBACK}. "
        f"Consider clearing Cline context (Cmd+Shift+P -> 'Cline: New Task') "
        f"if the prior turns relied on Claude."
    )
    return (True, None)


def reset_state_for_tests() -> None:
    """Clear the module-local cache + one-time-warned flag so each
    test starts from a clean slate. Pytest fixture in tests/test_router.py
    calls this in setup/teardown.

    Not exported via __all__; intended for tests only."""
    global _cache, _warned_session
    with _state_lock:
        _cache = None
        _warned_session = False
