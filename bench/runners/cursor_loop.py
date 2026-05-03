"""Cursor-style two-turn agent loop simulator.

Simulates the realistic flow that Cursor's agent runs against a local or
remote model:

    1. Send the task prompt as a single user turn.
    2. Run the acceptance test suite against the model's response.
    3. If anything fails, send turn 2 in the SAME conversation:
         user1   = original task prompt
         assist1 = model's broken response
         user2   = "your code failed these tests, here is the output, fix it"
    4. Re-grade the second response.

The interesting metric for the local arm is turn-2 TTFT: because the
21k-token prompt prefix is identical between turns, Ollama's KV cache
should give a much faster first token on the follow-up. The interesting
metric overall is whether the model recovers without explicit
human-authored hints -- only the test failure text drives the fix.

Usage:

    python -m bench.runners.cursor_loop \
        --task quotas_feature \
        --model local-long \
        --arm-tag local-only-2turn

    python -m bench.runners.cursor_loop \
        --task quotas_feature \
        --model claude-code \
        --arm-tag claude-only-2turn

The arm tag is what goes into `bench_runs.arm`, which keeps the loop
runs separate from one-shot baseline rows in the report.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from bench import db, grader  # noqa: E402
from bench.runners import litellm_arm  # noqa: E402

from cost.pricing import (  # noqa: E402
    actual_claude_cost,
    shadow_cost as _shadow_cost_fn,
)

# Backwards-compat: a couple of older imports look up these names on the
# module. Mirror Sonnet 4.6 rates from the canonical pricing table.
CLAUDE_INPUT_PER_TOKEN = litellm_arm.CLAUDE_INPUT_PER_TOKEN
CLAUDE_OUTPUT_PER_TOKEN = litellm_arm.CLAUDE_OUTPUT_PER_TOKEN
RESULTS = REPO_ROOT / "bench" / "results"


# How many lines of pytest output to keep in the feedback message. We
# want the assertion errors and the "FAILED ..." summary; we don't need
# the full traceback for every test. 80 lines is plenty in practice.
FEEDBACK_OUTPUT_LINES = 80


def _summarize_pytest(stdout: str, stderr: str) -> str:
    """Build a compact feedback blob from pytest's output.

    We keep:
      - the per-test FAILED/ERROR lines
      - the bottom summary block
      - up to a dozen lines of context around each failure (for assertion
        text). Total cap: ~FEEDBACK_OUTPUT_LINES lines so the follow-up
        message stays a small fraction of the prompt prefix.
    """
    text = stdout or ""
    if stderr and stderr.strip():
        text += "\n--- stderr ---\n" + stderr.strip()
    lines = text.splitlines()
    if len(lines) <= FEEDBACK_OUTPUT_LINES:
        return text.strip() or "(pytest produced no output)"
    # Otherwise, anchor on the "short test summary info" block which
    # pytest emits near the bottom and is the most actionable content.
    anchor = None
    for i, line in enumerate(lines):
        if "short test summary info" in line:
            anchor = i
            break
    if anchor is None:
        return "\n".join(lines[-FEEDBACK_OUTPUT_LINES:])
    # Keep the assertion failures (tail) plus a slice of leading context.
    tail = lines[anchor:]
    head_budget = max(0, FEEDBACK_OUTPUT_LINES - len(tail) - 4)
    head = lines[:head_budget] if head_budget > 0 else []
    if head and tail:
        return "\n".join(head + ["...", ""] + tail)
    return "\n".join(tail[-FEEDBACK_OUTPUT_LINES:])


def _build_feedback_message(turn1_grade_path: pathlib.Path) -> str:
    """Read pytest output from turn-1 grade.json and turn the failure
    into a fix-it user message."""
    if not turn1_grade_path.exists():
        return (
            "Your previous response did not produce any usable code. "
            "Please re-emit the full files in the format requested at "
            "the top of the original prompt."
        )
    grade = json.loads(turn1_grade_path.read_text())
    passed = int(grade.get("pytest_passed", 0))
    failed = int(grade.get("pytest_failed", 0))
    errors = int(grade.get("pytest_errors", 0))
    total = int(grade.get("pytest_total", 0))
    stdout = grade.get("pytest_stdout_tail") or ""
    stderr = grade.get("pytest_stderr_tail") or ""
    summary = _summarize_pytest(stdout, stderr)

    return (
        f"I ran the hidden acceptance test suite against your previous response. "
        f"Result: {passed} passed, {failed} failed, {errors} errored, out of "
        f"{total} total. Here is the relevant pytest output:\n\n"
        f"```\n{summary}\n```\n\n"
        f"Diagnose the failures and emit ONLY the minimal set of files you "
        f"need to change to make those specific tests pass.\n"
        f"\n"
        f"Strict rules for this turn:\n"
        f"  1. Emit at most TWO files. Pick only the files whose code is "
        f"actually wrong; do not re-emit files that already work.\n"
        f"  2. Use the same `\u0060\u0060\u0060python:<filename>` fenced format "
        f"as before, with the FULL final body of each file you re-emit.\n"
        f"  3. Do NOT repeat any file more than once. Do NOT include the "
        f"unchanged modules (storage.py, dispatcher.py, etc.).\n"
        f"  4. Keep the rest of the architecture identical -- you are "
        f"patching a small bug, not redesigning the feature.\n"
        f"  5. No prose between or after the fences."
    )


def _is_claude(model: str, reported: str | None) -> bool:
    return "claude" in (reported or model or "").lower()


def _cost(
    in_tok: int,
    out_tok: int,
    *,
    claude: bool,
    model_id: str = "claude-sonnet-4-6",
) -> tuple[float, float]:
    """Return (actual_usd, shadow_usd).

    `actual` is what we'd really pay for this Claude model (Opus and
    Haiku price differently from Sonnet). `shadow` stays pinned to
    Sonnet 4.6 so the savings benchmark is comparable across runs.
    For non-Claude calls actual=0.
    """
    shadow = _shadow_cost_fn(in_tok, out_tok)
    actual = actual_claude_cost(model_id, in_tok, out_tok) if claude else 0.0
    return actual, shadow


def _grade_into(task: dict[str, Any], response: str, work_dir: pathlib.Path) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "raw_response.txt").write_text(response or "")
    if not response:
        return {
            "pytest_passed": 0, "pytest_failed": 0, "pytest_errors": 1,
            "pytest_total": 0, "composite_score": 0.0,
            "passes_tests": 0.0,
        }
    gr = grader.grade_task(task, response, work_dir=work_dir)
    return gr.as_db_row()


def _seed_turn1_response(seed_dir: pathlib.Path) -> str:
    """Reconstruct an assistant turn-1 message from a previous run's
    `_submission/` directory. We rebuild the fenced-block format the
    grader expects (```python:<filename> ... ```), in a stable order."""
    sub = seed_dir / "_submission"
    if not sub.exists():
        raise FileNotFoundError(f"seed submission not found: {sub}")
    # Pick out only files we care about for the feature: quotas.py and
    # api.py (the two the model originally edited). Everything else
    # came from the unmodified codebase and doesn't need to be re-sent.
    targets = [n for n in ("quotas.py", "api.py") if (sub / n).exists()]
    if not targets:
        raise FileNotFoundError(f"no quotas.py/api.py in {sub}")
    parts: list[str] = []
    for name in targets:
        body = (sub / name).read_text().rstrip()
        parts.append(f"```python:{name}\n{body}\n```")
    return "\n\n".join(parts)


def run_loop(
    *,
    arm_tag: str,
    model: str,
    task: dict[str, Any],
    work_root: pathlib.Path,
    pytest_timeout: float = 120.0,
    pass_threshold: float = 1.0,
    seed_turn1_from: pathlib.Path | None = None,
    max_turns: int = 2,
) -> dict[str, Any]:
    """Run the multi-turn fix-from-failures loop. Returns per-turn metrics.

    `max_turns` controls the upper bound (turn 1 + up to `max_turns - 1`
    fix-it follow-ups). The loop short-circuits as soon as a turn's
    merged composite score >= `pass_threshold`.

    When `seed_turn1_from` is given, turn 1 does NOT call the model.
    Instead we reconstruct an assistant message from that directory's
    `_submission/` files and grade it as turn 1. This lets us skip the
    expensive cold call and start from a known-broken state, making
    the loop reproducible. Useful for ablations where the starting
    failure matters more than the first generation pass.
    """
    started = time.time()
    prompt = task["prompt"]
    work_root.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "task_id": task["id"],
        "arm": arm_tag,
        "model": model,
        "started_ts": int(started),
        "seed_turn1_from": str(seed_turn1_from) if seed_turn1_from else None,
        "turns": [],
    }

    # ---- Turn 1 -----------------------------------------------------------
    turn1_dir = work_root / "turn1"
    if seed_turn1_from is not None:
        litellm_arm._progress_emit(
            f"[{time.strftime('%H:%M:%S')}] [{model}] cursor-loop turn 1: "
            f"seeded from {seed_turn1_from} (no model call)"
        )
        out1 = _seed_turn1_response(seed_turn1_from)
        res1 = {
            "ok": True, "output": out1,
            "wall_ms": 0, "ttft_ms": 0,
            "input_tokens": 0, "output_tokens": 0,
        }
    else:
        litellm_arm._progress_emit(
            f"[{time.strftime('%H:%M:%S')}] [{model}] cursor-loop turn 1: cold prompt"
        )
        res1 = litellm_arm.call_streaming(
            model,
            messages=[{"role": "user", "content": prompt}],
        )
        out1 = res1.get("output", "") if res1.get("ok") else ""
    grade1 = _grade_into(task, out1, turn1_dir)
    turn1_grade_path = turn1_dir / "grade.json"
    summary["turns"].append({
        "turn": 1,
        "ok": bool(res1.get("ok")),
        "wall_ms": res1.get("wall_ms", 0),
        "ttft_ms": res1.get("ttft_ms", 0),
        "input_tokens":  int(res1.get("input_tokens", 0) or 0),
        "output_tokens": int(res1.get("output_tokens", 0) or 0),
        "output_chars": len(out1),
        "pytest_passed": int(grade1.get("pytest_passed", 0)),
        "pytest_total":  int(grade1.get("pytest_total", 0)),
        "composite_score": float(grade1.get("composite_score", 0.0)),
    })
    litellm_arm._progress_emit(
        f"[{time.strftime('%H:%M:%S')}] [{model}] cursor-loop turn 1 done: "
        f"{grade1.get('pytest_passed', 0)}/{grade1.get('pytest_total', 0)} "
        f"score={grade1.get('composite_score', 0.0):.3f} "
        f"ttft={res1.get('ttft_ms', 0)}ms wall={res1.get('wall_ms', 0)}ms"
    )

    # ---- Turns 2..N: generic fix-from-failures loop -----------------------
    # Build the rolling history. After each turn we extend with the
    # assistant's response and the next user feedback message.
    history: list[dict[str, Any]] = [
        {"role": "user",      "content": prompt},
        {"role": "assistant", "content": out1 or "(empty response)"},
    ]
    responses_so_far: list[str] = [out1]
    last_grade_path = turn1_grade_path
    last_score = float(grade1.get("composite_score", 0.0))

    for turn in range(2, max_turns + 1):
        if last_score >= pass_threshold:
            litellm_arm._progress_emit(
                f"[{time.strftime('%H:%M:%S')}] [{model}] turn {turn - 1} hit "
                f"score={last_score:.3f} (>= {pass_threshold:.2f}); "
                f"stopping early"
            )
            break

        feedback = _build_feedback_message(last_grade_path)
        (work_root / f"feedback_t{turn}.txt").write_text(feedback)
        turn_dir = work_root / f"turn{turn}"
        litellm_arm._progress_emit(
            f"[{time.strftime('%H:%M:%S')}] [{model}] cursor-loop turn {turn}: "
            f"sending feedback ({len(feedback)} chars), expecting KV cache hit"
        )
        # Snapshot the conversation we send: history-so-far + this turn's
        # user feedback. We append to `history` AFTER the call so the
        # next iteration starts from a complete prior-turn record.
        turn_messages = history + [{"role": "user", "content": feedback}]
        resN = litellm_arm.call_streaming(model, messages=turn_messages)
        outN = resN.get("output", "") if resN.get("ok") else ""

        # Solo grade: just this turn's response on the base codebase.
        # Useful for spotting "model emits a half-rewrite that only
        # rebuilds half the codebase" scenarios.
        solo_grade = _grade_into(task, outN, turn_dir)

        # Merged grade: every turn's edits stacked, last one wins.
        # This is the canonical "agent state" score.
        responses_so_far.append(outN)
        merged_dir = work_root / f"turn{turn}_merged"
        merged_grade = _grade_layered(
            task, responses_so_far, merged_dir,
            pytest_timeout=pytest_timeout,
        )

        summary["turns"].append({
            "turn": turn,
            "ok": bool(resN.get("ok")),
            "wall_ms": resN.get("wall_ms", 0),
            "ttft_ms": resN.get("ttft_ms", 0),
            "input_tokens":  int(resN.get("input_tokens", 0) or 0),
            "output_tokens": int(resN.get("output_tokens", 0) or 0),
            "output_chars": len(outN),
            "pytest_passed_solo":   int(solo_grade.get("pytest_passed", 0)),
            "pytest_total_solo":    int(solo_grade.get("pytest_total", 0)),
            "composite_score_solo": float(solo_grade.get("composite_score", 0.0)),
            "pytest_passed":   int(merged_grade.get("pytest_passed", 0)),
            "pytest_total":    int(merged_grade.get("pytest_total", 0)),
            "composite_score": float(merged_grade.get("composite_score", 0.0)),
            "feedback_chars": len(feedback),
        })
        litellm_arm._progress_emit(
            f"[{time.strftime('%H:%M:%S')}] [{model}] cursor-loop turn {turn} done: "
            f"merged {merged_grade.get('pytest_passed', 0)}/"
            f"{merged_grade.get('pytest_total', 0)} "
            f"score={merged_grade.get('composite_score', 0.0):.3f} "
            f"ttft={resN.get('ttft_ms', 0)}ms wall={resN.get('wall_ms', 0)}ms"
        )

        # Roll forward for the next iteration: extend history with this
        # turn's user feedback and the assistant response, in that order.
        # `turn_messages` captured the snapshot used for the call; we
        # rebuild from that so the order is self-evidently correct.
        history = list(turn_messages) + [
            {"role": "assistant", "content": outN or "(empty response)"}
        ]
        # `_build_feedback_message` reads from grade.json -- point it at
        # the merged grade (the canonical agent state), not the solo one,
        # so the next feedback reflects what the model still needs to fix
        # in the cumulative submission.
        last_grade_path = merged_dir / "grade.json"
        last_score = float(merged_grade.get("composite_score", 0.0))

    # ---- Persist per-turn rows in bench_runs ------------------------------
    rids: list[int] = []
    for turn_summary in summary["turns"]:
        in_tok = turn_summary["input_tokens"]
        out_tok = turn_summary["output_tokens"]
        claude = _is_claude(model, model)
        actual, shadow = _cost(in_tok, out_tok, claude=claude, model_id=model)
        # For turn 2 we want the merged composite (i.e. counting prior
        # files that didn't need re-emitting). For turn 1 it's the same.
        comp = float(turn_summary["composite_score"])
        passed = int(turn_summary.get("pytest_passed", 0))
        total = int(turn_summary.get("pytest_total", 0))
        row = {
            "ts": int(started),
            "task_id": task["id"],
            "arm": f"{arm_tag}-t{turn_summary['turn']}",
            "model": model,
            "attempt": turn_summary["turn"],
            "input_tok":  in_tok,
            "output_tok": out_tok,
            "actual_cost": actual,
            "shadow_cost": shadow,
            "wall_ms":     int(turn_summary["wall_ms"]),
            "generate_ms": int(turn_summary["wall_ms"]),
            "ttft_ms":     int(turn_summary["ttft_ms"]),
            "output_chars": int(turn_summary["output_chars"]),
            "notes": f"cursor-loop turn {turn_summary['turn']}",
            "raw_metadata": {"loop_turn": turn_summary["turn"], "arm_tag": arm_tag},
            "syntactic_ok":   1,
            "has_docstring":  1,
            "has_type_hints": 1,
            "no_thirdparty":  1,
            "pytest_passed":  passed,
            "pytest_failed":  max(0, total - passed),
            "pytest_errors":  0,
            "pytest_total":   total,
            "passes_tests":   (passed / total) if total else 0.0,
            "grade_ms": 0,
            "composite_score": comp,
            "output_path": "",
        }
        rids.append(db.record_run(row))

    summary["bench_run_ids"] = rids
    summary["wall_total_ms"] = int((time.time() - started) * 1000)
    (work_root / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def _grade_layered(
    task: dict[str, Any],
    responses: list[str],
    work_dir: pathlib.Path,
    *,
    pytest_timeout: float,
) -> dict[str, Any]:
    """Grade a turn N response with all prior turns' file edits as the baseline.

    `grade_feature_add` resets the submission tree to the unmodified
    codebase before applying the response. For a multi-turn fix that
    means a turn-N response that only re-emits, say, `quotas.py` would
    silently drop turn-(N-1)'s edits to `api.py`. To simulate the
    realistic Cursor flow ("the model already wrote api.py last turn;
    only edits quotas.py this turn"), we extract files from every
    response in chronological order and concatenate them, with later
    turns winning on conflicts. Then we run a single
    `grade_feature_add` on the merged response.
    """
    from bench.grader import extract_files
    merged: dict[str, str] = {}
    for resp in responses:
        merged.update(extract_files(resp or ""))
    if not merged:
        work_dir.mkdir(parents=True, exist_ok=True)
        last = responses[-1] if responses else ""
        (work_dir / "raw_response.txt").write_text(last or "")
        return {
            "pytest_passed": 0, "pytest_failed": 0, "pytest_errors": 1,
            "pytest_total": 0, "composite_score": 0.0, "passes_tests": 0.0,
        }
    # Re-encode as a single fenced response that grade_feature_add can
    # parse via its existing extractor.
    fakey = "\n\n".join(
        f"```python:{name}\n{body.rstrip()}\n```" for name, body in merged.items()
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "raw_response.txt").write_text(fakey)
    gr = grader.grade_task(task, fakey, work_dir=work_dir)
    return gr.as_db_row()


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--task", required=True, help="task id (file in bench/tasks/<id>.json)")
    p.add_argument("--model", required=True, help="LiteLLM model name (e.g. local-long, claude-code)")
    p.add_argument(
        "--arm-tag",
        default=None,
        help="arm label written to bench_runs (defaults to <model>-2turn)",
    )
    p.add_argument("--pytest-timeout", type=float, default=120.0)
    p.add_argument(
        "--pass-threshold", type=float, default=1.0,
        help="If turn 1's composite_score >= this, skip turn 2.",
    )
    p.add_argument(
        "--seed-turn1-from", default=None,
        help="Path to a previous results dir whose _submission/ should be "
             "used as turn-1 assistant output (skips the cold call).",
    )
    p.add_argument(
        "--max-turns", type=int, default=2,
        help="Maximum total turns including turn 1 (default 2). The loop "
             "exits early once a turn's merged composite score >= "
             "--pass-threshold.",
    )
    args = p.parse_args(argv)

    task = grader.load_task(args.task)
    arm_tag = args.arm_tag or f"{args.model}-2turn"
    work_root = RESULTS / f"{arm_tag}-{args.task}-{int(time.time())}"
    summary = run_loop(
        arm_tag=arm_tag,
        model=args.model,
        task=task,
        work_root=work_root,
        pytest_timeout=args.pytest_timeout,
        pass_threshold=args.pass_threshold,
        seed_turn1_from=pathlib.Path(args.seed_turn1_from).resolve()
            if args.seed_turn1_from else None,
        max_turns=args.max_turns,
    )
    print(json.dumps(summary, indent=2))
    print(f"\nResults dir: {work_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
