"""CLI: snapshot provider-billed spend for a benchmark arm/window into
`cost/cost.db.provider_spend`.

Usage:
  python -m bench.pull_spend \
      --arm claude-only \
      --task-id lru_ttl_cache \
      --window-start 1714000000 --window-end 1714003600 \
      --providers anthropic,cursor

Picks credentials up from env:
  ANTHROPIC_ADMIN_API_KEY   (admin key, required for Anthropic collector)
  ANTHROPIC_API_KEY_IDS     (optional, comma-separated to scope usage rows)
  CURSOR_ADMIN_API_KEY      (required for Cursor collector)
  CURSOR_USER_EMAILS        (optional, comma-separated to scope events)
  CURSOR_MANUAL_SPEND_CSV   (optional path; bypasses Cursor admin API and
                             ingests a manually-exported CSV instead - use this
                             on Pro / Pro+ individual accounts that don't have
                             team admin API access)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

from bench import db
from bench.collectors import anthropic_admin, cursor_admin


def _split(s: str | None) -> list[str]:
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--arm", required=True,
                   choices=["local-only", "claude-only", "cursor-no-proxy", "cursor-hybrid"])
    p.add_argument("--task-id", default="")
    p.add_argument("--window-start", type=int, required=True, help="unix sec")
    p.add_argument("--window-end",   type=int, required=True, help="unix sec")
    p.add_argument("--providers", default="anthropic,cursor",
                   help="comma-separated subset of {anthropic,cursor}")
    p.add_argument("--dry-run", action="store_true",
                   help="print counts but don't write to DB")
    args = p.parse_args(argv)

    providers = set(_split(args.providers))
    written = 0
    errors: list[str] = []

    if "anthropic" in providers:
        try:
            rows = anthropic_admin.collect(
                window_start=args.window_start,
                window_end=args.window_end,
                arm=args.arm,
                task_id=args.task_id,
                api_key_ids=_split(os.environ.get("ANTHROPIC_API_KEY_IDS")) or None,
            )
            print(f"[anthropic] {len(rows)} rows; "
                  f"billed=${sum(r.get('billed_usd', 0.0) for r in rows):.4f}")
            if not args.dry_run:
                for r in rows:
                    db.record_provider_spend(r)
                    written += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"anthropic: {e}")
            print(f"[anthropic] skipped: {e}", file=sys.stderr)

    if "cursor" in providers:
        manual_csv = os.environ.get("CURSOR_MANUAL_SPEND_CSV")
        try:
            if manual_csv:
                rows = cursor_admin.parse_manual_spend_csv(
                    manual_csv,
                    arm=args.arm, task_id=args.task_id,
                    window_start=args.window_start, window_end=args.window_end,
                )
                print(f"[cursor] {len(rows)} rows from manual CSV {manual_csv}; "
                      f"billed=${sum(r['billed_usd'] for r in rows):.4f}")
            else:
                rows = cursor_admin.collect(
                    window_start=args.window_start,
                    window_end=args.window_end,
                    arm=args.arm,
                    task_id=args.task_id,
                    user_emails=_split(os.environ.get("CURSOR_USER_EMAILS")) or None,
                )
                print(f"[cursor] {len(rows)} rows from admin API; "
                      f"billed=${sum(r.get('billed_usd', 0.0) for r in rows):.4f}")
            if not args.dry_run:
                for r in rows:
                    db.record_provider_spend(r)
                    written += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"cursor: {e}")
            print(f"[cursor] skipped: {e}", file=sys.stderr)

    print(f"\nwrote {written} provider_spend rows; "
          f"errors: {errors if errors else 'none'}")
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
