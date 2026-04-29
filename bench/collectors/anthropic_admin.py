"""Pull authoritative spend + token usage from Anthropic for a [start, end]
window. Uses the Admin Usage & Cost API:

  GET https://api.anthropic.com/v1/organizations/usage_report/messages
  GET https://api.anthropic.com/v1/organizations/cost_report

Auth: `x-api-key: <ANTHROPIC_ADMIN_API_KEY>`  (admin key, format
`sk-ant-admin...`). The standard `ANTHROPIC_API_KEY` will NOT work — the admin
key is a separate credential issued from the org settings page.

Reference: https://docs.anthropic.com/en/api/usage-cost-api
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Any, Iterable

import httpx

API_BASE = "https://api.anthropic.com/v1/organizations"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_TIMEOUT = 30.0


class AnthropicAdminError(RuntimeError):
    pass


def _iso(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _client(api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=API_BASE,
        timeout=DEFAULT_TIMEOUT,
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
    )


def _get_admin_key(env: dict[str, str] | None = None) -> str:
    e = env if env is not None else dict(os.environ)
    key = e.get("ANTHROPIC_ADMIN_API_KEY") or ""
    if not key:
        raise AnthropicAdminError(
            "ANTHROPIC_ADMIN_API_KEY is not set. Generate an Admin API key at "
            "console.anthropic.com → Settings → Admin API keys."
        )
    return key


def fetch_messages_usage(
    *,
    window_start: int,
    window_end: int,
    api_key_ids: Iterable[str] | None = None,
    bucket_width: str = "1d",
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Token-level usage. Returns one normalized row per time bucket."""
    key = api_key or _get_admin_key()
    params: dict[str, Any] = {
        "starting_at": _iso(window_start),
        "ending_at":   _iso(window_end),
        "bucket_width": bucket_width,
    }
    if api_key_ids:
        params["api_key_ids[]"] = list(api_key_ids)

    own = client is None
    cli = client or _client(key)
    try:
        r = cli.get("/usage_report/messages", params=params)
        r.raise_for_status()
        body = r.json()
    finally:
        if own:
            cli.close()

    out: list[dict[str, Any]] = []
    for bucket in body.get("data") or []:
        bs = bucket.get("starting_at") or ""
        be = bucket.get("ending_at") or ""
        for entry in bucket.get("results") or []:
            out.append({
                "bucket_start": bs,
                "bucket_end":   be,
                "model":        entry.get("model", ""),
                "api_key_id":   entry.get("api_key_id", "") or "",
                "uncached_input_tokens":  int(entry.get("uncached_input_tokens", 0) or 0),
                "cache_read_input_tokens":   int(entry.get("cache_read_input_tokens", 0) or 0),
                "cache_creation_input_tokens": int(entry.get("cache_creation_input_tokens", 0) or 0),
                "output_tokens":           int(entry.get("output_tokens", 0) or 0),
                "service_tier":            entry.get("service_tier", ""),
                "context_window":          entry.get("context_window", ""),
            })
    return out


def fetch_cost(
    *,
    window_start: int,
    window_end: int,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """USD spend per day. The cost endpoint only supports `1d` buckets."""
    key = api_key or _get_admin_key()
    params: dict[str, Any] = {
        "starting_at": _iso(window_start),
        "ending_at":   _iso(window_end),
        "bucket_width": "1d",
    }
    own = client is None
    cli = client or _client(key)
    try:
        r = cli.get("/cost_report", params=params)
        r.raise_for_status()
        body = r.json()
    finally:
        if own:
            cli.close()

    out: list[dict[str, Any]] = []
    for bucket in body.get("data") or []:
        bs = bucket.get("starting_at") or ""
        be = bucket.get("ending_at") or ""
        for entry in bucket.get("results") or []:
            cents = entry.get("amount_cents")
            if cents is None:
                # Some responses use {"amount": {"currency": "USD", "value_cents": N}}
                amt = entry.get("amount") or {}
                cents = amt.get("value_cents") or 0
            out.append({
                "bucket_start": bs,
                "bucket_end":   be,
                "billed_usd":   float(cents) / 100.0,
                "currency":     "USD",
                "raw":          entry,
            })
    return out


def collect(
    *,
    window_start: int,
    window_end: int,
    arm: str,
    task_id: str = "",
    api_key_ids: Iterable[str] | None = None,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Convenience: pull both usage + cost and return rows shaped for
    `bench.db.record_provider_spend`. One row per (api_key_id, model) for usage,
    one rolled-up row for cost (Anthropic doesn't break cost down by api_key).
    """
    usage = fetch_messages_usage(
        window_start=window_start, window_end=window_end,
        api_key_ids=api_key_ids, api_key=api_key,
    )
    cost = fetch_cost(
        window_start=window_start, window_end=window_end, api_key=api_key,
    )

    rows: list[dict[str, Any]] = []
    for u in usage:
        rows.append({
            "arm": arm, "task_id": task_id,
            "window_start": window_start, "window_end": window_end,
            "provider": "anthropic", "source": "admin-api",
            "input_tok":  u["uncached_input_tokens"],
            "output_tok": u["output_tokens"],
            "cache_read_tok":  u["cache_read_input_tokens"],
            "cache_write_tok": u["cache_creation_input_tokens"],
            "requests": 0,
            "billed_usd": 0.0,
            "api_key_id": u["api_key_id"],
            "raw_response": u,
        })

    total_billed = sum(c["billed_usd"] for c in cost)
    rows.append({
        "arm": arm, "task_id": task_id,
        "window_start": window_start, "window_end": window_end,
        "provider": "anthropic", "source": "admin-api-cost",
        "billed_usd": float(total_billed),
        "raw_response": {"daily": cost},
    })
    return rows
