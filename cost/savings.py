"""CLI savings report. Prints today / 7d / month-to-date summaries.

Usage:
  python3 cost/savings.py            # all three windows
  python3 cost/savings.py 7          # last N days
  python3 cost/savings.py --json     # machine-readable
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from cost.ingest import connect  # noqa: E402


def _window(days: int | None) -> tuple[int, int]:
    now = int(time.time())
    if days is None:
        return (0, now)
    return (now - days * 86400, now)


def summarize(days: int | None) -> dict[str, Any]:
    start, end = _window(days)
    conn = connect()
    rows = conn.execute(
        """
        SELECT tier,
               COUNT(*)            AS n,
               COALESCE(SUM(input_tok),0)  AS in_tok,
               COALESCE(SUM(output_tok),0) AS out_tok,
               COALESCE(SUM(actual_cost),0) AS actual,
               COALESCE(SUM(shadow_cost),0) AS shadow,
               COALESCE(AVG(latency_ms),0)  AS avg_ms
        FROM requests
        WHERE ts BETWEEN ? AND ?
        GROUP BY tier
        """,
        (start, end),
    ).fetchall()
    conn.close()

    by_tier = {r["tier"]: dict(r) for r in rows}
    total_n      = sum(r["n"]      for r in rows)
    total_in     = sum(r["in_tok"]  for r in rows)
    total_out    = sum(r["out_tok"] for r in rows)
    total_actual = sum(r["actual"]  for r in rows)
    total_shadow = sum(r["shadow"]  for r in rows)
    savings      = total_shadow - total_actual
    pct = (savings / total_shadow * 100.0) if total_shadow > 0 else 0.0
    return {
        "window_days": days,
        "total_requests": total_n,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "actual_spend_usd": round(total_actual, 4),
        "shadow_spend_usd": round(total_shadow, 4),
        "savings_usd": round(savings, 4),
        "savings_pct": round(pct, 2),
        "by_tier": {
            t: {
                "requests": r["n"],
                "input_tokens":  r["in_tok"],
                "output_tokens": r["out_tok"],
                "actual_usd": round(r["actual"], 4),
                "shadow_usd": round(r["shadow"], 4),
                "avg_latency_ms": int(r["avg_ms"]),
            }
            for t, r in by_tier.items()
        },
    }


def _fmt_usd(v: float) -> str:
    return f"${v:,.2f}"


def _print_block(title: str, s: dict[str, Any]) -> None:
    print(f"\n=== {title} ===")
    print(f"Requests:       {s['total_requests']:,}")
    by_tier = s["by_tier"]
    parts = [f"{tier} {info['requests']:,}" for tier, info in by_tier.items()]
    if parts:
        print(f"  by tier:      " + " / ".join(parts))
    print(f"Tokens in/out:  {s['total_input_tokens']:,} / {s['total_output_tokens']:,}")
    print(f"Actual spend:   {_fmt_usd(s['actual_spend_usd'])}")
    print(f"Shadow spend:   {_fmt_usd(s['shadow_spend_usd'])}")
    print(f"Savings:        {_fmt_usd(s['savings_usd'])} ({s['savings_pct']:.1f}%)")


def main(argv: list[str]) -> int:
    if "--json" in argv:
        days_arg = next((a for a in argv if a.isdigit()), None)
        days = int(days_arg) if days_arg else 7
        print(json.dumps(summarize(days), indent=2))
        return 0

    if len(argv) >= 1 and argv[0].isdigit():
        days = int(argv[0])
        _print_block(f"Last {days} days", summarize(days))
        return 0

    _print_block("Today",          summarize(1))
    _print_block("Last 7 days",    summarize(7))
    _print_block("Last 30 days",   summarize(30))
    _print_block("All time",       summarize(None))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
