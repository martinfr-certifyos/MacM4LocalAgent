"""bench.grader: code extraction + AST checks + score computation.

The pytest subprocess path is exercised end-to-end via a tiny synthetic task
so we don't need to depend on the real `lru_ttl_cache` task spec being solved.
"""
from __future__ import annotations

import json
import pathlib
import textwrap

import pytest

from bench import grader


# ---- extract_python ---------------------------------------------------------

def test_extract_python_picks_first_python_fence() -> None:
    text = "intro\n```python\nprint('hi')\n```\nblah\n```python\nprint('lo')\n```\n"
    assert grader.extract_python(text).strip() == "print('hi')"


def test_extract_python_falls_back_to_any_fence() -> None:
    text = "```\nprint('plain')\n```"
    assert grader.extract_python(text).strip() == "print('plain')"


def test_extract_python_falls_back_to_raw_text() -> None:
    text = "x = 1\nprint(x)"
    assert grader.extract_python(text).strip() == "x = 1\nprint(x)"


# ---- AST checks --------------------------------------------------------------

def test_check_syntactic_true_and_false() -> None:
    assert grader._check_syntactic("def f(): return 1") is True
    assert grader._check_syntactic("def f( return") is False


def test_module_docstring_word_count() -> None:
    short = '"""tiny"""\n'
    longer = '"""' + ("word " * 80) + '"""\n'
    assert grader._has_module_docstring(short) is False
    assert grader._has_module_docstring(longer) is True


def test_no_third_party_imports() -> None:
    allowed = {"collections", "threading", "time"}
    ok = "import threading\nfrom collections import OrderedDict\n"
    bad = "import requests\n"
    assert grader._no_third_party_imports(ok, allowed) is True
    assert grader._no_third_party_imports(bad, allowed) is False


def test_public_funcs_have_type_hints_threshold() -> None:
    code = textwrap.dedent("""
        def public_a(x: int) -> int: return x
        def public_b(y: int) -> int: return y
        def _private(z): return z
    """)
    assert grader._public_funcs_have_type_hints(code) is True

    code_bad = textwrap.dedent("""
        def public_a(x): return x
        def public_b(y): return y
        def public_c(z): return z
    """)
    assert grader._public_funcs_have_type_hints(code_bad) is False


# ---- end-to-end grade() with a synthetic task -------------------------------

@pytest.fixture
def synthetic_task(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Build a self-contained task whose grader is two passing assertions."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    test_file = tasks_dir / "test_addone.py"
    test_file.write_text(textwrap.dedent("""
        import addone
        def test_one(): assert addone.add(1) == 2
        def test_two(): assert addone.add(10) == 11
    """).lstrip())

    task = {
        "id": "addone",
        "prompt": "irrelevant",
        "grading": {
            "save_as": "addone.py",
            "test_file": "test_addone.py",
            "weights": {
                "passes_tests": 0.9,
                "syntactic_validity": 0.05,
                "docstring": 0.05,
            },
        },
    }
    # Persist as JSON too so `grader.load_task("addone")` finds it.
    import json as _json
    (tasks_dir / "addone.json").write_text(_json.dumps(task))
    monkeypatch.setattr(grader, "TASKS_DIR", tasks_dir, raising=True)

    return task


def test_grade_full_pass(synthetic_task, tmp_path: pathlib.Path) -> None:
    code = textwrap.dedent('''
        """A tiny module that adds one. We document the choice of int over
        float here just to satisfy the docstring check; production code would
        have a much richer rationale than this example sentence does in tests
        repeatedly to push the word count past the threshold of sixty words
        and beyond, ensuring the module docstring metric ticks over to true
        for the purposes of the grader's ast inspection of the module."""
        def add(x: int) -> int:
            return x + 1
    ''').lstrip()
    work = tmp_path / "work"
    res = grader.grade(synthetic_task, code, work_dir=work)
    assert res.syntactic_ok is True
    assert res.pytest_total == 2
    assert res.pytest_passed == 2
    assert res.pytest_failed == 0
    assert res.composite_score >= 0.95


def test_grade_partial_fail(synthetic_task, tmp_path: pathlib.Path) -> None:
    code = textwrap.dedent('''
        """Wrong implementation. Sixty words of justification follow here so
        that the module docstring threshold check passes regardless of the
        implementation correctness; this matters because we're measuring the
        composite scorer's behavior on partially-correct submissions, not
        only on the all-pass case which would be trivially boring as a
        single test of the harness end to end."""
        def add(x: int) -> int:
            return x  # off-by-one
    ''').lstrip()
    work = tmp_path / "work"
    res = grader.grade(synthetic_task, code, work_dir=work)
    assert res.syntactic_ok is True
    assert res.pytest_total == 2
    assert res.pytest_passed == 0
    assert res.pytest_failed == 2
    assert 0.0 < res.composite_score < 0.95


def test_grade_syntax_error_skips_pytest(synthetic_task, tmp_path: pathlib.Path) -> None:
    code = "def add(x: int) -> int return x + 1\n"  # missing colon
    work = tmp_path / "work"
    res = grader.grade(synthetic_task, code, work_dir=work)
    assert res.syntactic_ok is False
    assert res.pytest_total == 0
    assert res.pytest_errors == 1
    assert res.composite_score < 0.5


# ---- extract_json -----------------------------------------------------------

def test_extract_json_picks_json_fence_first() -> None:
    text = '```json\n{"a": 1}\n```\nthen ```json\n{"a": 2}\n```'
    assert grader.extract_json(text) == {"a": 1}


def test_extract_json_falls_back_to_bracket_scan() -> None:
    # The fallback prefers the first balanced object/array. With production
    # bug-hunt responses we always wrap in {"bugs": [...]} which is found
    # by the {-scan first.
    text = 'preamble then {"bugs":[{"file":"x.py"}, {"file":"y.py"}]} trailing prose'
    assert grader.extract_json(text) == {
        "bugs": [{"file": "x.py"}, {"file": "y.py"}]
    }


def test_extract_json_returns_none_on_no_json() -> None:
    assert grader.extract_json("just prose, nothing structured") is None


# ---- bug_hunt grader --------------------------------------------------------

@pytest.fixture
def bug_hunt_task(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "gt.json").write_text(json.dumps({
        "bugs": [
            {
                "id":       "B1",
                "file":     "storage.py",
                "function": "move_to_dead_letter",
                "summary":  "no DELETE",
                "keywords": ["delete", "deliveries", "loop"],
                "severity": "critical",
                "category": "correctness",
            },
            {
                "id":       "B2",
                "file":     "signing.py",
                "function": "verify",
                "summary":  "not constant time",
                "keywords": ["constant", "time", "compare_digest"],
                "severity": "high",
                "category": "security",
            },
            {
                "id":       "B3",
                "file":     "rate_limiter.py",
                "function": "allow",
                "summary":  "lru inverted",
                "keywords": ["move_to_end", "OrderedDict", "lru"],
                "severity": "medium",
                "category": "correctness",
            },
        ],
    }))
    task = {
        "id": "bug_hunt_test",
        "prompt": "find bugs",
        "grading": {
            "kind":         "bug_hunt",
            "ground_truth": "gt.json",
            "weights": {
                "f1":             0.5,
                "found_critical": 0.3,
                "precision":      0.1,
                "valid_json":     0.1,
            },
        },
    }
    monkeypatch.setattr(grader, "TASKS_DIR", tasks_dir, raising=True)
    return task


def test_bug_hunt_strong_match_perfect_recall(
    bug_hunt_task, tmp_path: pathlib.Path,
) -> None:
    response = '```json\n' + json.dumps({"bugs": [
        {"file": "storage.py",      "function": "move_to_dead_letter",
         "severity": "critical",    "summary": "DLQ does not delete"},
        {"file": "signing.py",      "function": "verify",
         "severity": "high",        "summary": "comparison not constant time"},
        {"file": "rate_limiter.py", "function": "allow",
         "severity": "medium",      "summary": "LRU eviction is inverted"},
    ]}) + '\n```'
    res = grader.grade_task(bug_hunt_task, response, work_dir=tmp_path / "w")
    assert res.pytest_passed == 3
    assert res.pytest_total == 3
    assert res.pytest_failed == 0
    assert res.composite_score >= 0.95


def test_bug_hunt_partial_with_false_positives(
    bug_hunt_task, tmp_path: pathlib.Path,
) -> None:
    response = '```json\n' + json.dumps({"bugs": [
        # hits B1 strongly
        {"file": "storage.py", "function": "move_to_dead_letter",
         "summary": "still in deliveries, infinite loop"},
        # plausible-looking false positive
        {"file": "models.py", "function": "to_dict",
         "summary": "doesn't sort keys"},
        # fake file/function
        {"file": "nope.py", "function": "no_such",
         "summary": "fictional"},
    ]}) + '\n```'
    res = grader.grade_task(bug_hunt_task, response, work_dir=tmp_path / "w")
    assert res.pytest_passed == 1
    assert res.pytest_total == 3
    assert res.pytest_failed == 2
    assert 0.0 < res.composite_score < 0.8


def test_bug_hunt_no_json_scores_zero(
    bug_hunt_task, tmp_path: pathlib.Path,
) -> None:
    response = "I think there might be some issues but I can't be sure."
    res = grader.grade_task(bug_hunt_task, response, work_dir=tmp_path / "w")
    assert res.syntactic_ok is False  # repurposed as valid_json
    assert res.pytest_passed == 0
    assert res.pytest_total == 3
    assert res.composite_score == 0.0


def test_bug_hunt_keyword_medium_match(
    bug_hunt_task, tmp_path: pathlib.Path,
) -> None:
    """File matches but function name is wrong; rich keyword overlap saves it."""
    response = '```json\n' + json.dumps({"bugs": [
        {"file": "rate_limiter.py", "function": "wrong_name_here",
         "summary": "the OrderedDict eviction does not call move_to_end "
                    "for new buckets so LRU semantics are broken"},
    ]}) + '\n```'
    res = grader.grade_task(bug_hunt_task, response, work_dir=tmp_path / "w")
    assert res.pytest_passed == 1
    assert res.pytest_failed == 0


# ---- extract_files (multi-file) --------------------------------------------

def test_extract_files_path_header_form() -> None:
    text = (
        "Here are the files.\n"
        "```python:quotas.py\n"
        "def get_quota(): return 0\n"
        "```\n"
        "```python:api.py\n"
        "X = 1\n"
        "```\n"
    )
    files = grader.extract_files(text)
    assert set(files.keys()) == {"quotas.py", "api.py"}
    assert "def get_quota" in files["quotas.py"]
    assert "X = 1" in files["api.py"]


def test_extract_files_file_comment_form() -> None:
    text = (
        "```python\n"
        "# FILE: storage.py\n"
        "import sqlite3\n"
        "```\n"
    )
    files = grader.extract_files(text)
    assert list(files.keys()) == ["storage.py"]
    assert "import sqlite3" in files["storage.py"]


def test_extract_files_bare_path_comment_form() -> None:
    text = (
        "```python\n"
        "# quotas.py\n"
        "X = 1\n"
        "```\n"
    )
    files = grader.extract_files(text)
    assert "quotas.py" in files


def test_extract_files_strips_directory_traversal() -> None:
    text = (
        "```python:../../etc/evil.py\n"
        "X = 1\n"
        "```\n"
    )
    files = grader.extract_files(text)
    # The leading ../ is stripped to a flat path; the writer further
    # collapses to basename, so the file ends up as a benign etc/evil.py
    # which the grader will then write under work_dir.
    assert "../" not in next(iter(files.keys()))


def test_extract_files_returns_empty_on_no_fences() -> None:
    assert grader.extract_files("just prose, no code") == {}


# ---- feature_add grader -----------------------------------------------------

@pytest.fixture
def feature_add_task(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A tiny self-contained feature-add task: base codebase has a `lib.py`
    with a `multiply` function, the model is asked to add a `divide` helper
    in a new module `extra.py`, and the test file checks both."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    codebase = tasks_dir / "tiny_codebase"
    codebase.mkdir()
    (codebase / "lib.py").write_text(
        "def multiply(a: int, b: int) -> int:\n"
        "    return a * b\n"
    )
    (tasks_dir / "test_extra.py").write_text(
        "import sys, pathlib\n"
        "sub = pathlib.Path(__file__).parent / '_submission'\n"
        "sys.path.insert(0, str(sub))\n"
        "import lib, extra\n"
        "def test_multiply_unchanged(): assert lib.multiply(3, 4) == 12\n"
        "def test_divide(): assert extra.divide(10, 2) == 5\n"
        "def test_divide_zero():\n"
        "    import pytest as _pytest\n"
        "    with _pytest.raises(ZeroDivisionError):\n"
        "        extra.divide(1, 0)\n"
    )
    task = {
        "id": "tiny_feature",
        "prompt": "irrelevant",
        "grading": {
            "kind":         "feature_add",
            "codebase_dir": "tiny_codebase",
            "test_file":    "test_extra.py",
            "weights": {
                "passes_tests":           0.8,
                "syntactic_validity":     0.05,
                "docstring":              0.05,
                "type_hints":             0.05,
                "no_third_party_imports": 0.05,
            },
        },
    }
    monkeypatch.setattr(grader, "TASKS_DIR", tasks_dir, raising=True)
    return task


def test_feature_add_full_pass(feature_add_task, tmp_path: pathlib.Path) -> None:
    response = (
        '```python:extra.py\n'
        '"""Helpers added on top of lib.py. The divide function preserves '
        'the standard ZeroDivisionError so callers can use try/except."""\n'
        'def divide(a: int, b: int) -> int:\n'
        '    return a // b\n'
        '```\n'
    )
    res = grader.grade_task(feature_add_task, response, work_dir=tmp_path / "w")
    assert res.pytest_total == 3
    assert res.pytest_passed == 3
    assert res.composite_score >= 0.95


def test_feature_add_partial_fail(feature_add_task, tmp_path: pathlib.Path) -> None:
    """Wrong implementation returns 1 instead of dividing."""
    response = (
        '```python:extra.py\n'
        '"""A wrong implementation that ignores its arguments. Documented '
        'here purely so the docstring quality check still passes for our test."""\n'
        'def divide(a: int, b: int) -> int:\n'
        '    return 1\n'
        '```\n'
    )
    res = grader.grade_task(feature_add_task, response, work_dir=tmp_path / "w")
    assert res.pytest_total == 3
    assert res.pytest_passed == 1
    assert 0.0 < res.composite_score < 0.6


def test_feature_add_no_files_extracted(feature_add_task, tmp_path: pathlib.Path) -> None:
    res = grader.grade_task(feature_add_task, "I don't know how", work_dir=tmp_path / "w")
    assert res.pytest_total == 0
    assert res.composite_score == 0.0


def test_feature_add_syntax_error_short_circuits(feature_add_task, tmp_path: pathlib.Path) -> None:
    response = (
        '```python:extra.py\n'
        'def divide(a, b: oops syntax\n'
        '```\n'
    )
    res = grader.grade_task(feature_add_task, response, work_dir=tmp_path / "w")
    assert res.syntactic_ok is False
    assert res.pytest_total == 0  # short-circuited
    assert res.pytest_errors == 1
