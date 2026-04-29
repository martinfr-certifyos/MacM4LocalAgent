"""Print and/or emit JSON summarizing benchmark runs across the three arms.

For each (task_id, arm), reports:
  - attempts, pass_rate (pytest-based), mean composite score
  - median wall_ms, median generate_ms, median ttft_ms
  - tokens (in/out)
  - actual cost (locally instrumented) AND provider-billed cost from
    `provider_spend` (Anthropic admin API for claude-only, Cursor admin API
    or manual CSV for cursor-* arms)
  - delta % vs the cheapest arm

Usage:
  python -m bench.report --task lru_ttl_cache
  python -m bench.report --task lru_ttl_cache --json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from typing import Any

from bench import db


def _median(xs: list[int]) -> int:
    return int(statistics.median(xs)) if xs else 0


def summarize_task(task_id: str) -> dict[str, Any]:
    rows = db.list_runs(task_id=task_id)
    if not rows:
        return {"task_id": task_id, "arms": {}, "rows": 0}

    by_arm: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_arm.setdefault(r["arm"], []).append(dict(r))

    out: dict[str, dict[str, Any]] = {}
    for arm, runs in by_arm.items():
        # Separate runs that produced gradable output from runs that errored
        # mid-flight (pytest_total == 0 means the subprocess never collected
        # any tests, usually because the call was interrupted, the model
        # produced no code, or the response stream was truncated).
        graded = [r for r in runs if r["pytest_total"] > 0]
        incomplete = [r for r in runs if r["pytest_total"] == 0]

        wall = [r["wall_ms"] for r in graded] or [r["wall_ms"] for r in runs]
        gen  = [r["generate_ms"] for r in graded] or [r["generate_ms"] for r in runs]
        ttft = [r["ttft_ms"] for r in graded] or [r["ttft_ms"] for r in runs]

        # pass_rate = full-pass attempts / graded attempts. If nothing was
        # graded, leave it at 0 and flag via `incomplete_runs`.
        full_passes = [
            1 if r["pytest_passed"] == r["pytest_total"] else 0
            for r in graded
        ]
        # Per-test pass fraction (more useful than all-or-nothing for partial
        # solutions like "20/22" -- shows up as a 0.91 average instead of 0).
        test_pass_fraction = [
            r["pytest_passed"] / r["pytest_total"]
            for r in graded
        ]
        scores = [r["composite_score"] for r in graded] or [
            r["composite_score"] for r in runs
        ]

        # Provider-billed: sum across the union of run windows for this arm.
        if runs:
            window_start = min(r["ts"] for r in runs)
            window_end   = max(r["ts"] + max(1, r["wall_ms"] // 1000) for r in runs)
            ps = db.provider_spend_for_window(
                arm=arm, window_start=window_start, window_end=window_end,
            )
        else:
            ps = {"by_provider": [], "total_billed_usd": 0.0}

        # Pick a single "headline" provider-billed number for this arm.
        # Prefer the most-specific source (usage-events) over the rollup.
        billed_specific = 0.0
        billed_specific_source = ""
        for pp in ps["by_provider"]:
            if pp["source"] in ("usage-events", "manual-csv", "admin-api-cost"):
                if pp["billed"] and pp["billed"] >= billed_specific:
                    billed_specific = float(pp["billed"])
                    billed_specific_source = pp["source"]
        billed_headline = (
            billed_specific
            if billed_specific
            else float(ps["total_billed_usd"])
        )

        out[arm] = {
            "attempts":      len(runs),
            "graded_attempts":     len(graded),
            "incomplete_runs":     len(incomplete),
            "pytest_pass_rate":    round(sum(full_passes) / len(graded), 3)
                                   if graded else 0.0,
            "mean_test_pass_pct":  round(
                statistics.mean(test_pass_fraction) * 100.0, 1
            ) if test_pass_fraction else 0.0,
            "mean_score": round(statistics.mean(scores), 3) if scores else 0.0,
            "median_wall_ms": _median(wall),
            "median_gen_ms":  _median(gen),
            "median_ttft_ms": _median(ttft),
            "total_input_tok":  sum(r["input_tok"] for r in runs),
            "total_output_tok": sum(r["output_tok"] for r in runs),
            "total_actual_usd": round(sum(r["actual_cost"] for r in runs), 6),
            "total_shadow_usd": round(sum(r["shadow_cost"] for r in runs), 6),
            "provider_billed_usd": round(billed_headline, 6),
            "provider_billed_source": billed_specific_source or (
                ps["by_provider"][0]["source"] if ps["by_provider"] else ""
            ),
            "provider_billed_by_source": ps["by_provider"],
        }

    # Compute deltas vs cheapest arm by *provider_billed_usd* (falling back to
    # actual when no provider data is anchored yet).
    def _ref_cost(a: dict[str, Any]) -> float:
        return a["provider_billed_usd"] or a["total_actual_usd"]

    if out:
        cheapest = min(_ref_cost(a) for a in out.values())
        for a in out.values():
            mine = _ref_cost(a)
            a["delta_vs_cheapest_usd"] = round(mine - cheapest, 6)
            a["delta_vs_cheapest_pct"] = (
                round((mine - cheapest) / cheapest * 100.0, 2) if cheapest > 0 else None
            )

    return {"task_id": task_id, "arms": out, "rows": len(rows)}


def _print_human(report: dict[str, Any]) -> None:
    task_id = report["task_id"]
    arms = report["arms"]
    print(f"\n=== Bench: {task_id}  ({report['rows']} runs across {len(arms)} arms) ===\n")
    if not arms:
        print("(no runs yet)")
        return

    cols = (
        "arm", "n", "graded", "full✓%", "tests%", "score",
        "wall_ms", "gen_ms", "ttft_ms",
        "in_tok", "out_tok", "actual$", "billed$ (provider)", "Δ vs cheapest",
    )
    fmt = ("  {:<16} {:>3} {:>6} {:>6} {:>6} {:>5} "
           "{:>8} {:>8} {:>8} {:>10} {:>10} {:>9} {:>22} {:>17}")
    print(fmt.format(*cols))
    print("  " + "-" * 150)
    for arm, a in arms.items():
        billed = a["provider_billed_usd"]
        billed_str = (
            f"${billed:.4f} ({a['provider_billed_source']})"
            if billed else "(not anchored)"
        )
        delta_pct = a.get("delta_vs_cheapest_pct")
        delta_str = (
            f"+${a['delta_vs_cheapest_usd']:.4f}/+{delta_pct:.1f}%"
            if delta_pct is not None and delta_pct > 0
            else "baseline"
        )
        graded_label = (
            f"{a['graded_attempts']}/{a['attempts']}"
            if a["incomplete_runs"]
            else f"{a['graded_attempts']}"
        )
        print(fmt.format(
            arm,
            a["attempts"],
            graded_label,
            f"{a['pytest_pass_rate']*100:.0f}%",
            f"{a['mean_test_pass_pct']:.0f}%",
            f"{a['mean_score']:.2f}",
            a["median_wall_ms"],
            a["median_gen_ms"],
            a["median_ttft_ms"],
            a["total_input_tok"],
            a["total_output_tok"],
            f"${a['total_actual_usd']:.4f}",
            billed_str,
            delta_str,
        ))

    print("\nNotes:")
    print("  - 'actual$' is what we instrumented locally (Claude rate card).")
    print("  - 'billed$' is what the provider actually charged for the run window")
    print("    (Anthropic Admin API for claude-only; Cursor Admin API or manual")
    print("    CSV for cursor-* arms). 0 means provider data wasn't pulled yet -")
    print("    run `python -m bench.pull_spend ...`.")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--task", required=True)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    rep = summarize_task(args.task)
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        _print_human(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
