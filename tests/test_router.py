"""Unit tests for the size + complexity-based router callback."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from router.complexity_classifier import classify
from router.route_by_size import (
    SizeBasedRouter,
    _estimate_tokens,
    _extract_user_task,
    _flat_prompt,
    _looks_like_failure,
    _sticky_escalations,
    _task_fingerprint,
    decide_tier,
    decide_tier_cline,
    ROUTE_FAST_MAX,
    ROUTE_LONG_MAX,
)


# ---- complexity_classifier ----------------------------------------------------

@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("Refactor the entire architecture",   True),
        ("Design a system for billing",        True),
        ("change across multiple files",       True),
        ("[claude] handle this please",        True),
        ("Think step by step about this",      True),
        ("[local] just do it fast",            False),
        ("add 1 to x",                         False),
        ("",                                   False),
    ],
)
def test_classify(prompt: str, expected: bool) -> None:
    is_complex, _ = classify(prompt)
    assert is_complex is expected


def test_classify_local_tag_overrides_complex() -> None:
    is_complex, reason = classify("[local] refactor the architecture please")
    assert is_complex is False
    assert "[local]" in reason


# ---- token estimator + flat_prompt --------------------------------------------

def test_estimate_tokens_empty() -> None:
    assert _estimate_tokens(None) == 0
    assert _estimate_tokens([]) == 0


def test_estimate_tokens_string_content() -> None:
    msgs = [{"role": "user", "content": "x" * 360}]
    # 360 / 3.6 = 100 tokens
    assert _estimate_tokens(msgs) == 100


def test_estimate_tokens_list_content() -> None:
    msgs = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "abc"},
            {"type": "image", "image": "..."},        # ignored
            {"type": "text", "text": "defg"},
        ],
    }]
    assert _estimate_tokens(msgs) >= 1


def test_flat_prompt_concats() -> None:
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user",   "content": "hi"},
    ]
    assert "sys" in _flat_prompt(msgs)
    assert "hi"  in _flat_prompt(msgs)


# ---- decide_tier --------------------------------------------------------------

def test_decide_tier_routes_small_to_fast() -> None:
    msgs = [{"role": "user", "content": "what does this regex do?"}]
    model, reason, tokens = decide_tier(msgs)
    assert model == "local-fast"
    assert tokens <= ROUTE_FAST_MAX


def test_decide_tier_routes_medium_to_long() -> None:
    chars = (ROUTE_FAST_MAX + 1000) * 4   # comfortably above the fast limit
    msgs = [{"role": "user", "content": "x" * chars}]
    model, reason, tokens = decide_tier(msgs)
    assert model == "local-long"
    assert ROUTE_FAST_MAX < tokens <= ROUTE_LONG_MAX


def test_decide_tier_routes_huge_to_claude() -> None:
    chars = (ROUTE_LONG_MAX + 5000) * 4
    msgs = [{"role": "user", "content": "x" * chars}]
    model, reason, tokens = decide_tier(msgs)
    assert model == "claude-code"
    assert "tokens" in reason


def test_decide_tier_complex_short_goes_claude() -> None:
    msgs = [{"role": "user", "content": "Refactor the architecture across multiple files"}]
    model, reason, _ = decide_tier(msgs)
    assert model == "claude-code"
    assert "complex" in reason


# ---- SizeBasedRouter callback -------------------------------------------------

@pytest.fixture
def router(tmp_db) -> SizeBasedRouter:                                       # noqa: ARG001
    return SizeBasedRouter()


def test_pre_call_rewrites_hybrid_auto(router: SizeBasedRouter) -> None:
    data: dict[str, Any] = {
        "model": "hybrid-auto",
        "messages": [{"role": "user", "content": "tiny prompt"}],
    }
    new = asyncio.run(router.async_pre_call_hook(None, None, data, "completion"))
    assert new is not None
    assert new["model"] in {"local-fast", "local-long", "claude-code"}
    meta = new["metadata"]
    assert meta["route_decision"] == new["model"]
    assert isinstance(meta["route_reason"], str)
    assert meta["route_tokens_estimated"] >= 1


def test_pre_call_does_not_touch_explicit_model(router: SizeBasedRouter) -> None:
    data: dict[str, Any] = {
        "model": "claude-code",
        "messages": [{"role": "user", "content": "hello"}],
    }
    new = asyncio.run(router.async_pre_call_hook(None, None, data, "completion"))
    assert new["model"] == "claude-code"
    assert "metadata" not in new or "route_decision" not in new.get("metadata", {})


# ---- gpt-* prefix strip (Cursor-friendly aliases) ----------------------------

@pytest.mark.parametrize(
    "incoming,expected_canonical",
    [
        ("gpt-local-fast",   "local-fast"),
        ("gpt-local-long",   "local-long"),
        ("gpt-local-agent",  "local-agent"),
        ("gpt-claude-code",  "claude-code"),
    ],
)
def test_pre_call_strips_gpt_prefix_for_explicit_aliases(
    router: SizeBasedRouter, incoming: str, expected_canonical: str
) -> None:
    """Cursor sends the OpenAI-shaped alias name; the router must rewrite
    it to the canonical name so cost-tier classification, the over-gen
    controls, and any downstream logic see the model they expect."""
    data: dict[str, Any] = {
        "model": incoming,
        "messages": [{"role": "user", "content": "hi"}],
    }
    new = asyncio.run(router.async_pre_call_hook(None, None, data, "completion"))
    assert new is not None
    assert new["model"] == expected_canonical


def test_pre_call_strips_gpt_prefix_then_rewrites_hybrid_auto(
    router: SizeBasedRouter,
) -> None:
    """gpt-hybrid-auto must collapse to hybrid-auto AND then trigger the
    size-based rewrite, exactly like the canonical alias does."""
    data: dict[str, Any] = {
        "model": "gpt-hybrid-auto",
        "messages": [{"role": "user", "content": "tiny prompt"}],
    }
    new = asyncio.run(router.async_pre_call_hook(None, None, data, "completion"))
    assert new is not None
    assert new["model"] in {"local-fast", "local-long", "claude-code"}
    meta = new["metadata"]
    assert meta["route_decision"] == new["model"]
    assert isinstance(meta["route_reason"], str)
    assert meta["route_tokens_estimated"] >= 1


def test_pre_call_does_not_strip_unknown_gpt_prefix(
    router: SizeBasedRouter,
) -> None:
    """A real OpenAI model name like `gpt-4o` should pass through
    untouched -- the strip is whitelisted to our specific aliases."""
    data: dict[str, Any] = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "hi"}],
    }
    new = asyncio.run(router.async_pre_call_hook(None, None, data, "completion"))
    assert new["model"] == "gpt-4o"


class _FakeUsage:
    def __init__(self, in_tok: int, out_tok: int) -> None:
        self.prompt_tokens = in_tok
        self.completion_tokens = out_tok

    def model_dump(self) -> dict[str, int]:
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens}


class _FakeResponse:
    def __init__(self, in_tok: int, out_tok: int) -> None:
        self.usage = _FakeUsage(in_tok, out_tok)


def test_log_success_event_records_local(router: SizeBasedRouter, tmp_db) -> None:
    start = time.time()
    router.log_success_event(
        kwargs={"model": "local-fast", "metadata": {"route_reason": "tokens 100 <= 16000"}},
        response_obj=_FakeResponse(100, 50),
        start_time=start,
        end_time=start + 0.3,
    )
    rows = list(router._conn.execute("SELECT * FROM requests"))
    assert len(rows) == 1
    r = dict(zip([d[0] for d in router._conn.execute("SELECT * FROM requests").description], rows[0]))
    assert r["tier"] == "local-fast"
    assert r["actual_cost"] == 0.0
    # shadow_cost = 100*3e-6 + 50*15e-6 = 0.0003 + 0.00075 = 0.00105
    assert r["shadow_cost"] == pytest.approx(0.00105, rel=1e-6)
    assert 200 <= r["latency_ms"] <= 600


def test_log_success_event_reads_route_reason_from_litellm_params(
    router: SizeBasedRouter, tmp_db,
) -> None:
    """LiteLLM relocates async_pre_call_hook metadata under
    kwargs['litellm_params']['metadata'] in the success-callback
    lifecycle. Without checking this nested path, the route_reason
    column is silently empty for every Cline + hybrid-auto request --
    which is exactly the bug we hit in production."""
    start = time.time()
    router.log_success_event(
        kwargs={
            "model": "local-long",
            # NO top-level metadata; only the nested litellm_params copy.
            "litellm_params": {
                "metadata": {
                    "route_decision": "local-long",
                    "route_reason": "cline-mode: cline+default: task=12 tok",
                    "route_tokens_estimated": 12,
                },
            },
        },
        response_obj=_FakeResponse(15000, 200),
        start_time=start,
        end_time=start + 1.5,
    )
    row = router._conn.execute(
        "SELECT route_reason FROM requests WHERE model='local-long'"
    ).fetchone()
    assert row is not None
    assert row[0] == "cline-mode: cline+default: task=12 tok"


def test_log_success_event_top_metadata_takes_priority_when_set(
    router: SizeBasedRouter, tmp_db,
) -> None:
    """If both top-level and nested metadata are present (rare but
    possible if a different LiteLLM lifecycle path applies), prefer
    the nested one because that's where the routing decision actually
    flows from. This pins the precedence so a future edit can't
    silently flip it."""
    start = time.time()
    router.log_success_event(
        kwargs={
            "model": "local-fast",
            "metadata": {"route_reason": "TOP-LEVEL"},
            "litellm_params": {"metadata": {"route_reason": "NESTED"}},
        },
        response_obj=_FakeResponse(10, 5),
        start_time=start,
        end_time=start + 0.1,
    )
    row = router._conn.execute(
        "SELECT route_reason FROM requests WHERE model='local-fast'"
    ).fetchone()
    assert row is not None
    assert row[0] == "NESTED"


def test_log_success_event_records_claude(router: SizeBasedRouter) -> None:
    start = time.time()
    router.log_success_event(
        kwargs={"model": "claude-sonnet-4-6"},
        response_obj=_FakeResponse(1000, 500),
        start_time=start,
        end_time=start + 1.2,
    )
    rows = list(router._conn.execute(
        "SELECT tier, actual_cost, shadow_cost FROM requests WHERE model='claude-sonnet-4-6'"
    ))
    assert len(rows) == 1
    tier, actual, shadow = rows[0]
    assert tier == "claude"
    # actual == shadow for Claude calls.
    assert actual == pytest.approx(shadow, rel=1e-6)
    assert actual == pytest.approx(1000 * 3e-6 + 500 * 15e-6, rel=1e-6)


def test_log_success_event_dict_response(router: SizeBasedRouter) -> None:
    """LiteLLM sometimes hands back a dict instead of an object."""
    start = time.time()
    router.log_success_event(
        kwargs={"model": "ollama/qwen3-coder:30b"},
        response_obj={"usage": {"prompt_tokens": 10, "completion_tokens": 20}},
        start_time=start,
        end_time=start + 0.05,
    )
    (tier, in_tok, out_tok) = router._conn.execute(
        "SELECT tier, input_tok, output_tok FROM requests WHERE model LIKE 'ollama/%'"
    ).fetchone()
    assert tier == "local-long"
    assert in_tok == 10
    assert out_tok == 20


# ---- Cline-aware routing -----------------------------------------------------
#
# Cline ships a ~13.5K-token system prompt; size-based routing alone always
# picks local-long for it regardless of how trivial the task is, AND the
# complexity classifier accidentally matches `[local]` substrings inside
# Cline's tool documentation. The Cline-aware path extracts the user's
# task from `<task>...</task>` and classifies on THAT only.

# A minimal-but-realistic Cline system prompt: contains the fingerprints
# `_looks_like_cline` checks for. We deliberately keep this short so test
# token-count assertions are predictable.
_CLINE_SYSTEM = (
    "You are Cline, a highly skilled software engineer. "
    "Use <replace_in_file> to edit files and <attempt_completion> to "
    "signal completion."
)


def _cline_msgs(task: str, *extra: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a Cline-shaped messages array: system + user(<task>...) + extras."""
    base: list[dict[str, Any]] = [
        {"role": "system", "content": _CLINE_SYSTEM},
        {"role": "user", "content": f"<task>\n{task}\n</task>"},
    ]
    base.extend(extra)
    return base


@pytest.fixture(autouse=True)
def _clear_sticky():
    """Stickiness leaks between tests because it's module-level state.
    Clear before AND after each test so we don't accidentally route a
    later test to claude because an earlier test marked the same task
    fingerprint."""
    _sticky_escalations.clear()
    yield
    _sticky_escalations.clear()


def test_extract_user_task_pulls_text_from_envelope() -> None:
    msgs = _cline_msgs("Add a comment to README")
    assert _extract_user_task(msgs) == "Add a comment to README"


def test_extract_user_task_handles_list_content() -> None:
    msgs = [
        {"role": "system", "content": _CLINE_SYSTEM},
        {
            "role": "user",
            "content": [{"type": "text", "text": "<task>\nFix bug\n</task>"}],
        },
    ]
    assert _extract_user_task(msgs) == "Fix bug"


def test_extract_user_task_returns_none_for_non_cline() -> None:
    msgs = [{"role": "user", "content": "just a regular question"}]
    assert _extract_user_task(msgs) is None


def test_extract_user_task_returns_none_for_empty() -> None:
    assert _extract_user_task(None) is None
    assert _extract_user_task([]) is None


def test_decide_tier_cline_default_routes_to_local_long() -> None:
    msgs = _cline_msgs("Add a single-line comment to main.py")
    tier, reason, _ = decide_tier_cline(msgs)
    assert tier == "local-long"
    assert "default" in reason


def test_decide_tier_cline_complexity_keyword_escalates() -> None:
    msgs = _cline_msgs("Refactor the entire authentication architecture")
    tier, reason, _ = decide_tier_cline(msgs)
    assert tier == "claude-code"
    assert "architecture" in reason or "design" in reason


def test_decide_tier_cline_claude_tag_escalates() -> None:
    msgs = _cline_msgs("[claude] What is 2+2?")
    tier, reason, _ = decide_tier_cline(msgs)
    assert tier == "claude-code"
    assert "[claude]" in reason


def test_decide_tier_cline_local_tag_overrides_complex_keywords() -> None:
    """[local] is the user opting out -- it must beat complexity AND
    failure detection. This is the cost-safety guarantee for users
    who deliberately want to exercise the local stack."""
    msgs = _cline_msgs(
        "[local] Refactor the entire authentication architecture across multiple files"
    )
    tier, reason, _ = decide_tier_cline(msgs)
    assert tier == "local-long"
    assert "[local]" in reason


def test_decide_tier_cline_local_tag_overrides_failure_signal() -> None:
    """Even on a failing turn, [local] keeps us on local."""
    failure = (
        "[read_file] Result:\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 5, in <module>\n'
        "    raise ValueError\n"
        "ValueError: oops"
    )
    msgs = _cline_msgs(
        "[local] Add a test",
        {"role": "assistant", "content": "I'll read the file."},
        {"role": "user", "content": failure},
    )
    tier, reason, _ = decide_tier_cline(msgs)
    assert tier == "local-long"
    assert "[local]" in reason


def test_decide_tier_cline_python_traceback_escalates_on_turn3() -> None:
    failure = (
        "[read_file] Result:\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 5, in <module>\n'
        "    raise ValueError\n"
        "ValueError: oops"
    )
    msgs = _cline_msgs(
        "Add a test for the parser",
        {"role": "assistant", "content": "I'll read the file."},
        {"role": "user", "content": failure},
    )
    tier, reason, _ = decide_tier_cline(msgs)
    assert tier == "claude-code"
    assert "traceback" in reason


def test_decide_tier_cline_big_file_dump_does_not_escalate() -> None:
    """Cline's normal environment_details / read_file payload is several KB.
    Pure size must NOT trigger escalation -- only actual error signatures."""
    big_dump = "[read_file] Result:\n" + (
        "def boring_function():\n    return 42\n" * 500
    )
    msgs = _cline_msgs(
        "Read the file and summarize",
        {"role": "assistant", "content": "Reading."},
        {"role": "user", "content": big_dump},
    )
    tier, reason, _ = decide_tier_cline(msgs)
    assert tier == "local-long"
    assert "default" in reason


def test_decide_tier_cline_first_turn_does_not_check_tool_result() -> None:
    """Tool-result detection requires turn 2+ (msg_count >= 4). On turn 1
    we have just system + task, which CAN'T have a failure."""
    msgs = _cline_msgs("Add a test")
    tier, reason, _ = decide_tier_cline(msgs)
    assert tier == "local-long"
    assert "default" in reason


def test_decide_tier_cline_sticky_keeps_task_on_claude() -> None:
    """Once a task escalates, ALL subsequent turns of the same task
    stay on Claude even if those turns themselves look trivial."""
    # First call: complexity escalates -> claude.
    msgs1 = _cline_msgs("Refactor the entire authentication architecture")
    tier1, _, _ = decide_tier_cline(msgs1)
    assert tier1 == "claude-code"

    # Second call: same task, but with a clean tool result. Should still
    # be claude because the task fingerprint is sticky.
    msgs2 = _cline_msgs(
        "Refactor the entire authentication architecture",
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "[read_file] Result: file is empty"},
    )
    tier2, reason2, _ = decide_tier_cline(msgs2)
    assert tier2 == "claude-code"
    assert "sticky" in reason2


def test_decide_tier_cline_different_task_resets_stickiness() -> None:
    """Stickiness is per-task-fingerprint -- a NEW task gets a fresh
    decision."""
    msgs1 = _cline_msgs("Refactor the architecture across multiple files")
    decide_tier_cline(msgs1)  # marks task1 sticky

    msgs2 = _cline_msgs("Add a one-line comment to README")
    tier2, reason2, _ = decide_tier_cline(msgs2)
    assert tier2 == "local-long"
    assert "default" in reason2


def test_task_fingerprint_normalizes_whitespace() -> None:
    """The same task with different whitespace (Cline indents
    inconsistently in the <task> envelope) must produce the same
    fingerprint, otherwise stickiness misses on every turn."""
    f1 = _task_fingerprint("Add a comment to README")
    f2 = _task_fingerprint("Add  a comment   to README")
    f3 = _task_fingerprint("Add\na comment\nto README")
    assert f1 == f2 == f3


@pytest.mark.parametrize(
    "text,is_failure",
    [
        ("just some output", False),
        ("Traceback (most recent call last):\n  File 'x'", True),
        ("thread 'main' panicked at 'oops', src/main.rs:5", True),
        (
            "stack:\n  at foo (file.js:5:3)\n  at bar (file.js:10:5)",
            True,
        ),
        ("error: file not found\nerror: cannot read\nerror: aborting", True),
        # Single 'error:' line is NOT enough -- Cline emits these in
        # benign log output sometimes.
        ("error: file not found", False),
        # JS stack with only one frame is suspicious but not strong
        # enough on its own.
        ("at foo (file.js:5:3)", False),
    ],
)
def test_looks_like_failure(text: str, is_failure: bool) -> None:
    got, _reason = _looks_like_failure(text)
    assert got is is_failure


def test_pre_call_uses_cline_aware_path_for_cline_traffic() -> None:
    """End-to-end: pre-call hook with hybrid-auto + Cline harness
    should route to local-long (default), not the legacy size-based
    path that would (accidentally) match [local] in the harness."""
    router = SizeBasedRouter()
    data: dict[str, Any] = {
        "model": "hybrid-auto",
        "messages": _cline_msgs("Add a comment to README"),
    }
    new = asyncio.run(router.async_pre_call_hook(None, None, data, "completion"))
    assert new is not None
    assert new["model"] == "local-long"
    meta = new["metadata"]
    assert "cline-mode" in meta["route_reason"]
    assert "default" in meta["route_reason"]


def test_pre_call_cline_complex_task_routes_to_claude() -> None:
    router = SizeBasedRouter()
    data: dict[str, Any] = {
        "model": "hybrid-auto",
        "messages": _cline_msgs(
            "Refactor the entire authentication architecture across multiple files"
        ),
    }
    new = asyncio.run(router.async_pre_call_hook(None, None, data, "completion"))
    assert new is not None
    assert new["model"] == "claude-code"
    assert "cline-mode" in new["metadata"]["route_reason"]


def test_pre_call_non_cline_traffic_uses_legacy_routing() -> None:
    """Non-Cline traffic (no Cline fingerprint in system prompt) must
    fall through to the existing size-based router -- otherwise we'd
    break the CLI/curl/benchmark callers."""
    router = SizeBasedRouter()
    data: dict[str, Any] = {
        "model": "hybrid-auto",
        "messages": [{"role": "user", "content": "small ask"}],
    }
    new = asyncio.run(router.async_pre_call_hook(None, None, data, "completion"))
    assert new is not None
    # Tiny non-Cline request -> local-fast (legacy path).
    assert new["model"] == "local-fast"
    # And the reason should NOT have the cline-mode prefix.
    assert "cline-mode" not in new["metadata"]["route_reason"]
