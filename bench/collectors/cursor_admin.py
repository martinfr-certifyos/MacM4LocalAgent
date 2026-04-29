"""Pull authoritative spend + per-event token usage from Cursor for a
[start, end] window.

This uses the Cursor Admin API (Teams/Enterprise plans). Auth is HTTP Basic
with the team API key as the username and an empty password. Endpoints used:

  POST https://api.cursor.com/teams/spend                  - cycle totals
  POST https://api.cursor.com/teams/filtered-usage-events  - per-event detail
                                                              (model, tokens,
                                                              chargedCents)
  POST https://api.cursor.com/teams/daily-usage-data       - per-day rollup

Reference: https://cursor.com/docs/account/teams/admin-api

For Pro / Pro+ individual accounts the Admin API is NOT available. In that
case `fetch_*` raises `CursorAdminError`; the runner falls back to the manual
paste flow (`bench/runners/cursor_session.py --spend-csv ...`).
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Any, Iterable

import httpx

API_BASE = "https://api.cursor.com"
DEFAULT_TIMEOUT = 30.0


class CursorAdminError(RuntimeError):
    pass


def _ms(ts: int) -> int:
    return int(ts) * 1000


def _client(api_key: str) -> httpx.Client:
    # Cursor uses Basic auth with the API key as username, empty password.
    return httpx.Client(
        base_url=API_BASE,
        timeout=DEFAULT_TIMEOUT,
        auth=(api_key, ""),
        headers={"content-type": "application/json"},
    )


def _get_key(env: dict[str, str] | None = None) -> str:
    e = env if env is not None else dict(os.environ)
    key = e.get("CURSOR_ADMIN_API_KEY") or ""
    if not key:
        raise CursorAdminError(
            "CURSOR_ADMIN_API_KEY is not set. Generate a team key at "
            "cursor.com → Dashboard → Team → API Keys (Teams/Enterprise only)."
        )
    return key


def fetch_spend(
    *,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Current billing-cycle spend totals (per team member).

    Returns: {"spendingMembers": [...], "totalCycleSpendCents": int}.
    """
    key = api_key or _get_key()
    own = client is None
    cli = client or _client(key)
    try:
        r = cli.post("/teams/spend", json={})
        r.raise_for_status()
        body = r.json()
    finally:
        if own:
            cli.close()
    members = body.get("teamMemberSpend") or body.get("spendingMembers") or []
    total = sum(int(m.get("spendCents", 0) or 0) for m in members)
    return {"members": members, "totalCycleSpendCents": total, "raw": body}


def fetch_usage_events(
    *,
    window_start: int,
    window_end: int,
    user_emails: Iterable[str] | None = None,
    api_key: str | None = None,
    client: httpx.Client | None = None,
    page_size: int = 100,
    max_pages: int = 50,
) -> list[dict[str, Any]]:
    """Per-event detail (one record per Cursor request) within the window.

    Documentation note: `chargedCents` is the field to sum to match dashboard
    totals. It includes both the model cost and the Cursor token rate.
    """
    key = api_key or _get_key()
    own = client is None
    cli = client or _client(key)
    out: list[dict[str, Any]] = []
    page = 1
    payload: dict[str, Any] = {
        "startDate": _ms(window_start),
        "endDate":   _ms(window_end),
        "pageSize":  page_size,
    }
    if user_emails:
        payload["userEmails"] = list(user_emails)
    try:
        while page <= max_pages:
            payload["page"] = page
            r = cli.post("/teams/filtered-usage-events", json=payload)
            r.raise_for_status()
            body = r.json()
            events = body.get("usageEvents") or []
            out.extend(events)
            total = int(body.get("totalUsageEventsCount") or 0)
            if not events or len(out) >= total:
                break
            page += 1
    finally:
        if own:
            cli.close()
    return out


def fetch_daily_usage(
    *,
    window_start: int,
    window_end: int,
    api_key: str | None = None,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Rolled-up daily usage (composer requests, cmdk usages, tokens, etc.)."""
    key = api_key or _get_key()
    own = client is None
    cli = client or _client(key)
    payload = {"startDate": _ms(window_start), "endDate": _ms(window_end)}
    try:
        r = cli.post("/teams/daily-usage-data", json=payload)
        r.raise_for_status()
        body = r.json()
    finally:
        if own:
            cli.close()
    return body.get("data") or []


def collect(
    *,
    window_start: int,
    window_end: int,
    arm: str,
    task_id: str = "",
    user_emails: Iterable[str] | None = None,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Pull per-event detail + cycle spend, return rows for record_provider_spend."""
    events = fetch_usage_events(
        window_start=window_start, window_end=window_end,
        user_emails=user_emails, api_key=api_key,
    )

    # Per-event row (the "ground truth" for the run window).
    rows: list[dict[str, Any]] = []
    sum_in = sum_out = sum_cr = sum_cw = 0
    sum_cents = 0
    by_model: dict[str, dict[str, int]] = {}
    for e in events:
        tu = e.get("tokenUsage") or {}
        in_t  = int(tu.get("inputTokens", 0) or 0)
        out_t = int(tu.get("outputTokens", 0) or 0)
        crd_t = int(tu.get("cacheReadTokens", 0) or 0)
        cwt_t = int(tu.get("cacheWriteTokens", 0) or 0)
        cents = int(e.get("chargedCents", 0) or 0)
        sum_in += in_t; sum_out += out_t; sum_cr += crd_t; sum_cw += cwt_t
        sum_cents += cents
        m = e.get("model", "") or "unknown"
        bucket = by_model.setdefault(m, {"in": 0, "out": 0, "cr": 0, "cw": 0, "cents": 0, "n": 0})
        bucket["in"]   += in_t
        bucket["out"]  += out_t
        bucket["cr"]   += crd_t
        bucket["cw"]   += cwt_t
        bucket["cents"] += cents
        bucket["n"]    += 1

    for model, b in by_model.items():
        rows.append({
            "arm": arm, "task_id": task_id,
            "window_start": window_start, "window_end": window_end,
            "provider": "cursor", "source": "usage-events",
            "input_tok":  b["in"],
            "output_tok": b["out"],
            "cache_read_tok":  b["cr"],
            "cache_write_tok": b["cw"],
            "requests":   b["n"],
            "billed_usd": b["cents"] / 100.0,
            "raw_response": {"model": model, "totals": b, "n_events": b["n"]},
        })

    # Window roll-up row.
    rows.append({
        "arm": arm, "task_id": task_id,
        "window_start": window_start, "window_end": window_end,
        "provider": "cursor", "source": "usage-events-rollup",
        "input_tok": sum_in, "output_tok": sum_out,
        "cache_read_tok": sum_cr, "cache_write_tok": sum_cw,
        "requests": len(events),
        "billed_usd": sum_cents / 100.0,
        "raw_response": {"event_count": len(events)},
    })
    return rows


def parse_manual_spend_csv(path: str, *, arm: str, task_id: str,
                           window_start: int, window_end: int) -> list[dict[str, Any]]:
    """Fallback for Pro accounts: ingest a CSV exported from
    cursor.com/dashboard → Billing & Invoices → Export.

    Expected columns (case-insensitive): timestamp, model, input_tokens,
    output_tokens, cache_read_tokens, cache_write_tokens, charged_usd.
    Missing columns are tolerated and treated as 0.
    """
    import csv
    rows: list[dict[str, Any]] = []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            r_norm = {k.lower().strip(): (v or "").strip() for k, v in r.items()}
            rows.append({
                "arm": arm, "task_id": task_id,
                "window_start": window_start, "window_end": window_end,
                "provider": "cursor", "source": "manual-csv",
                "input_tok":  int(r_norm.get("input_tokens", "0") or 0),
                "output_tok": int(r_norm.get("output_tokens", "0") or 0),
                "cache_read_tok":  int(r_norm.get("cache_read_tokens", "0") or 0),
                "cache_write_tok": int(r_norm.get("cache_write_tokens", "0") or 0),
                "requests": 1,
                "billed_usd": float(r_norm.get("charged_usd", "0") or 0.0),
                "raw_response": r_norm,
            })
    return rows
