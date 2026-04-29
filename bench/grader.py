"""Extract a model's submitted code and grade it.

Pipeline:
  1. Parse the model's chat completion text and pull out the *first* fenced
     ```python``` block. Fall back to the whole response.
  2. Write it to `bench/results/<run-id>/<save_as>` next to the task's grader
     test file.
  3. Run pytest in a subprocess, parse the JSON report, count pass/fail.
  4. Compute a composite score from the task's weight map.

Public surface:
  - extract_python(text) -> str
  - grade(task, code, *, work_dir) -> GradeResult
"""
from __future__ import annotations

import ast
import dataclasses
import json
import pathlib
import re
import shutil
import subprocess
import sys
import time
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TASKS_DIR = pathlib.Path(__file__).with_name("tasks")
RESULTS_DIR = pathlib.Path(__file__).with_name("results")

PY_FENCE = re.compile(r"```(?:python|py)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
JSON_FENCE = re.compile(r"```(?:json|JSON)\s*\n(.*?)```", re.DOTALL)
ANY_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\s*\n(.*?)```", re.DOTALL)


def extract_python(text: str) -> str:
    """Return the first python fenced block, or the first fenced block, or the
    raw text trimmed."""
    if not text:
        return ""
    m = PY_FENCE.search(text)
    if m:
        return m.group(1).rstrip() + "\n"
    m = ANY_FENCE.search(text)
    if m:
        return m.group(1).rstrip() + "\n"
    return text.strip() + "\n"


def extract_json(text: str) -> Any:
    """Find and parse the first JSON value in `text`. Tries (in order):

      1. A ```json ...``` fenced block.
      2. Any ``` ...``` fenced block whose body parses as JSON.
      3. The largest balanced {...} or [...] substring that parses.
      4. None on failure.
    """
    if not text:
        return None
    for matcher in (JSON_FENCE, ANY_FENCE):
        for m in matcher.finditer(text):
            body = m.group(1).strip()
            try:
                return json.loads(body)
            except (json.JSONDecodeError, ValueError):
                continue
    # Fallback: scan for a JSON object/array by bracket matching.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1:
            depth = 0
            in_string = False
            esc = False
            for i in range(start, len(text)):
                ch = text[i]
                if esc:
                    esc = False
                    continue
                if ch == "\\" and in_string:
                    esc = True
                    continue
                if ch == '"' and not esc:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i + 1]
                        try:
                            return json.loads(candidate)
                        except (json.JSONDecodeError, ValueError):
                            break
            start = text.find(opener, start + 1)
    return None


def load_task(task_id: str) -> dict[str, Any]:
    path = TASKS_DIR / f"{task_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"task spec not found: {path}")
    return json.loads(path.read_text())


@dataclasses.dataclass
class GradeResult:
    task_id: str
    work_dir: pathlib.Path
    code_path: pathlib.Path
    syntactic_ok: bool
    has_docstring: bool
    has_type_hints: bool
    no_thirdparty: bool
    pytest_passed: int
    pytest_failed: int
    pytest_errors: int
    pytest_total: int
    grade_ms: int
    composite_score: float
    raw_pytest_stdout: str
    raw_pytest_stderr: str

    def as_db_row(self) -> dict[str, Any]:
        passes_tests = (
            self.pytest_passed / self.pytest_total
            if self.pytest_total else 0.0
        )
        return {
            "syntactic_ok": int(self.syntactic_ok),
            "has_docstring": int(self.has_docstring),
            "has_type_hints": int(self.has_type_hints),
            "no_thirdparty": int(self.no_thirdparty),
            "pytest_passed": self.pytest_passed,
            "pytest_failed": self.pytest_failed,
            "pytest_errors": self.pytest_errors,
            "pytest_total":  self.pytest_total,
            "passes_tests":  passes_tests,
            "grade_ms":      self.grade_ms,
            "composite_score": self.composite_score,
            "output_path":   str(self.code_path),
        }


def _check_syntactic(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def _has_module_docstring(code: str, min_words: int = 60) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    doc = ast.get_docstring(tree) or ""
    return len(doc.split()) >= min_words


def _public_funcs_have_type_hints(code: str, min_ratio: float = 0.7) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    total = 0
    annotated = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            total += 1
            if node.returns is not None:
                annotated += 1
                continue
            if any(a.annotation is not None for a in node.args.args):
                annotated += 1
    return total == 0 or (annotated / total) >= min_ratio


def _no_third_party_imports(code: str, allowed: set[str]) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                if n.name.split(".")[0] not in allowed:
                    return False
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root and root not in allowed:
                return False
    return True


_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s or "")


def _run_pytest(work_dir: pathlib.Path, test_file: str, *, timeout: float = 120.0) -> tuple[int, int, int, int, str, str, int]:
    """Run pytest inside `work_dir`. Returns (passed, failed, errors, total,
    stdout, stderr, ms).

    We deliberately omit `-q` so the canonical summary line ("N passed,
    M failed, K error in T.TTs") is always emitted; with `-q` and failures
    pytest writes only the per-test FAILED list and the dotted progress, both
    of which are inconvenient to parse reliably.
    """
    started = time.time()
    cmd = [
        sys.executable, "-m", "pytest", test_file,
        "--tb=line", "--no-header", "-p", "no:cacheprovider",
        "--color=no",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(work_dir), capture_output=True, text=True,
            timeout=timeout,
        )
        out, err = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = f"pytest TIMEOUT after {timeout}s"
        return (0, 0, 1, 0, out, err, int((time.time() - started) * 1000))

    plain = _strip_ansi(out or "")
    passed = failed = errors = 0
    # Look for the summary line in either of pytest's two formats:
    #   "===== N passed, M failed, K error in T.TTs ====="
    #   "===== N passed in T.TTs ====="
    # (numbers may include thousands separators with comma; we accept digits.)
    p = re.search(r"(\d+)\s+passed", plain)
    if p:
        passed = int(p.group(1))
    f = re.search(r"(\d+)\s+failed", plain)
    if f:
        failed = int(f.group(1))
    e = re.search(r"(\d+)\s+error", plain)
    if e:
        errors = int(e.group(1))
    total = passed + failed + errors
    if total == 0 and proc.returncode != 0:
        # Pytest failed to even collect (e.g., import error) -- treat as 1 err.
        errors = 1
        total = 1
    return (passed, failed, errors, total, plain, err, int((time.time() - started) * 1000))


def grade_task(
    task: dict[str, Any],
    response_text: str,
    *,
    work_dir: pathlib.Path,
) -> GradeResult:
    """Dispatch to the correct grader based on `task['grading']['kind']`.

    Default kind is "pytest" (pull a python fence, run pytest). The
    bug-hunt tasks use kind="bug_hunt" which scores precision/recall of
    a JSON list of bug reports against a hidden ground-truth file.
    The "feature_add" kind layers the model's multi-file output on top
    of an existing codebase and runs an acceptance test suite.
    """
    kind = (task.get("grading") or {}).get("kind") or "pytest"
    if kind == "pytest":
        code = extract_python(response_text)
        return grade(task, code, work_dir=work_dir)
    if kind == "bug_hunt":
        return grade_bug_hunt(task, response_text, work_dir=work_dir)
    if kind == "feature_add":
        return grade_feature_add(task, response_text, work_dir=work_dir)
    raise ValueError(f"unknown grading kind: {kind}")


def grade(
    task: dict[str, Any],
    code: str,
    *,
    work_dir: pathlib.Path,
    pytest_timeout: float = 120.0,
) -> GradeResult:
    """Grade `code` against `task['grading']`. Writes the file + grader test
    into `work_dir`, runs pytest, and computes a composite [0, 1] score."""
    grading = task.get("grading") or {}
    save_as = grading.get("save_as", "submission.py")
    test_file = grading.get("test_file", "test_submission.py")
    weights = dict(grading.get("weights") or {})

    work_dir.mkdir(parents=True, exist_ok=True)
    code_path = work_dir / save_as
    code_path.write_text(code)

    # Copy the grader test file alongside.
    grader_src = TASKS_DIR / test_file
    if grader_src.exists():
        shutil.copy(grader_src, work_dir / test_file)
    else:
        raise FileNotFoundError(f"grader test file missing: {grader_src}")

    syntactic_ok   = _check_syntactic(code)
    has_docstring  = _has_module_docstring(code)
    has_type_hints = _public_funcs_have_type_hints(code)
    allowed_stdlib = {
        "collections", "threading", "time", "functools", "typing",
        "dataclasses", "weakref", "heapq", "math", "operator",
        "__future__", "abc", "sys",
    }
    no_thirdparty = _no_third_party_imports(code, allowed_stdlib)

    if syntactic_ok:
        passed, failed, errors, total, stdout, stderr, gms = _run_pytest(
            work_dir, test_file, timeout=pytest_timeout,
        )
    else:
        passed = failed = total = 0
        errors = 1
        stdout = ""
        stderr = "syntax error - skipped pytest"
        gms = 0

    passes_tests = passed / total if total else 0.0

    # Composite score (each [0,1], summed with task-supplied weights).
    feature = {
        "passes_tests":         passes_tests,
        "syntactic_validity":   1.0 if syntactic_ok else 0.0,
        "type_hints":           1.0 if has_type_hints else 0.0,
        "docstring":            1.0 if has_docstring else 0.0,
        "no_third_party_imports": 1.0 if no_thirdparty else 0.0,
        "stylistic":            1.0 if (syntactic_ok and code.count("\n") > 30) else 0.0,
    }
    score = 0.0
    weight_total = 0.0
    for k, w in weights.items():
        if k in feature:
            score += float(w) * feature[k]
            weight_total += float(w)
    if weight_total > 0:
        score /= weight_total

    return GradeResult(
        task_id=task["id"],
        work_dir=work_dir,
        code_path=code_path,
        syntactic_ok=syntactic_ok,
        has_docstring=has_docstring,
        has_type_hints=has_type_hints,
        no_thirdparty=no_thirdparty,
        pytest_passed=passed,
        pytest_failed=failed,
        pytest_errors=errors,
        pytest_total=total,
        grade_ms=gms,
        composite_score=round(score, 4),
        raw_pytest_stdout=stdout,
        raw_pytest_stderr=stderr,
    )


# --- bug-hunt grader --------------------------------------------------------

def _normalize_filename(s: Any) -> str:
    """Reduce 'storage.py', 'src/storage.py', 'webhookd/storage.py' all to
    'storage.py' for matching."""
    if not isinstance(s, str):
        return ""
    return s.strip().replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()


def _normalize_function(s: Any) -> str:
    if not isinstance(s, str):
        return ""
    return s.strip().lower().lstrip("_")


def _bug_text_blob(reported: dict[str, Any]) -> str:
    """Concatenate every text field of a reported bug into one lowercase
    blob, used for keyword matching."""
    parts: list[str] = []
    for k in ("summary", "description", "explanation", "details", "impact",
              "fix", "category", "severity", "title", "name", "id"):
        v = reported.get(k)
        if isinstance(v, str):
            parts.append(v)
    return " ".join(parts).lower()


def _match_bug(reported: dict[str, Any], truth: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether a reported bug matches a single ground-truth entry.

    Strong match: file matches AND function matches (substring either way).
    Medium match: file matches AND >=2 keywords from the ground truth appear
                  anywhere in the reported text fields.
    Returns (matched, kind) where kind is "strong", "medium", or "none".
    """
    rep_file = _normalize_filename(reported.get("file") or reported.get("filename") or reported.get("path"))
    rep_func = _normalize_function(
        reported.get("function") or reported.get("symbol") or reported.get("location") or "",
    )
    truth_file = _normalize_filename(truth["file"])
    truth_func = _normalize_function(truth["function"])

    if rep_file != truth_file:
        return (False, "none")

    if rep_func and truth_func and (
        rep_func == truth_func
        or rep_func in truth_func
        or truth_func in rep_func
    ):
        return (True, "strong")

    blob = _bug_text_blob(reported)
    keyword_hits = sum(1 for kw in truth.get("keywords") or [] if kw.lower() in blob)
    if keyword_hits >= 2:
        return (True, "medium")
    return (False, "none")


def _load_ground_truth(task: dict[str, Any]) -> list[dict[str, Any]]:
    grading = task.get("grading") or {}
    rel = grading.get("ground_truth")
    if not rel:
        raise ValueError("bug_hunt task missing grading.ground_truth")
    base = TASKS_DIR / rel
    if not base.exists():
        raise FileNotFoundError(f"ground truth not found: {base}")
    data = json.loads(base.read_text())
    bugs = data.get("bugs") if isinstance(data, dict) else data
    if not isinstance(bugs, list):
        raise ValueError("ground truth must contain a 'bugs' list")
    return bugs


def grade_bug_hunt(
    task: dict[str, Any],
    response_text: str,
    *,
    work_dir: pathlib.Path,
) -> GradeResult:
    """Score a bug-hunt response against the task's ground truth file.

    The model is expected to return JSON of the shape:
        {"bugs": [{"file": "...", "function": "...", "summary": "..."}, ...]}
    or a bare list of such objects.

    Composite score (each 0..1, summed by task weights):
        recall    = matched_truth / |truth|
        precision = matched_reports / |reports|
        f1        = 2 * P * R / (P + R)
        valid_json = 1 if extract_json found a list/dict
        no_hallucinations = matched_reports / |reports| (alias of precision)
    """
    started = time.time()
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path = work_dir / "response.txt"
    out_path.write_text(response_text or "")

    truth = _load_ground_truth(task)

    parsed = extract_json(response_text or "")
    valid_json = parsed is not None
    if isinstance(parsed, dict):
        reports_raw = parsed.get("bugs") or parsed.get("findings") or parsed.get("issues") or []
    elif isinstance(parsed, list):
        reports_raw = parsed
    else:
        reports_raw = []

    reports: list[dict[str, Any]] = [r for r in reports_raw if isinstance(r, dict)]

    matched_truth_ids: set[str] = set()
    matched_reports = 0
    per_truth_match_kind: dict[str, str] = {}
    report_match_log: list[dict[str, Any]] = []
    for r in reports:
        best: tuple[bool, str, dict[str, Any] | None] = (False, "none", None)
        for t in truth:
            if t["id"] in matched_truth_ids:
                continue
            ok, kind = _match_bug(r, t)
            if ok and (best[1] == "none" or (best[1] == "medium" and kind == "strong")):
                best = (True, kind, t)
                if kind == "strong":
                    break
        if best[0] and best[2] is not None:
            matched_truth_ids.add(best[2]["id"])
            per_truth_match_kind[best[2]["id"]] = best[1]
            matched_reports += 1
            report_match_log.append({
                "report_file": r.get("file"),
                "report_func": r.get("function") or r.get("symbol"),
                "matched_id":  best[2]["id"],
                "kind":        best[1],
            })
        else:
            report_match_log.append({
                "report_file": r.get("file"),
                "report_func": r.get("function") or r.get("symbol"),
                "matched_id":  None,
                "kind":        "none",
            })

    n_truth = len(truth)
    n_reports = len(reports)
    recall = (len(matched_truth_ids) / n_truth) if n_truth else 0.0
    precision = (matched_reports / n_reports) if n_reports else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    grading = task.get("grading") or {}
    weights = dict(grading.get("weights") or {})
    feature = {
        "f1":                f1,
        "recall":            recall,
        "precision":         precision,
        "valid_json":        1.0 if valid_json else 0.0,
        "found_critical":    _critical_found_ratio(truth, matched_truth_ids),
    }
    score = 0.0
    weight_total = 0.0
    for k, w in weights.items():
        if k in feature:
            score += float(w) * feature[k]
            weight_total += float(w)
    if weight_total > 0:
        score /= weight_total
    else:
        score = f1

    summary_path = work_dir / "grade.json"
    summary_path.write_text(json.dumps({
        "n_truth":           n_truth,
        "n_reports":         n_reports,
        "matched_reports":   matched_reports,
        "matched_truth_ids": sorted(matched_truth_ids),
        "missing_truth_ids": sorted(t["id"] for t in truth if t["id"] not in matched_truth_ids),
        "per_truth_match":   per_truth_match_kind,
        "report_match_log":  report_match_log,
        "recall":            round(recall, 4),
        "precision":         round(precision, 4),
        "f1":                round(f1, 4),
        "score":             round(score, 4),
        "valid_json":        valid_json,
    }, indent=2))

    grade_ms = int((time.time() - started) * 1000)
    # Repurpose the pytest_* slots so existing report code reads sensibly:
    #   pytest_passed = matched_truth_ids count
    #   pytest_total  = ground-truth bug count
    #   pytest_failed = unmatched reports (false positives)
    return GradeResult(
        task_id=task["id"],
        work_dir=work_dir,
        code_path=summary_path,
        syntactic_ok=valid_json,
        has_docstring=False,
        has_type_hints=False,
        no_thirdparty=True,
        pytest_passed=len(matched_truth_ids),
        pytest_failed=max(0, n_reports - matched_reports),
        pytest_errors=0,
        pytest_total=n_truth,
        grade_ms=grade_ms,
        composite_score=round(score, 4),
        raw_pytest_stdout=summary_path.read_text(),
        raw_pytest_stderr="",
    )


def _critical_found_ratio(
    truth: list[dict[str, Any]], matched_ids: set[str],
) -> float:
    crits = [t for t in truth if (t.get("severity") or "").lower() == "critical"]
    if not crits:
        return 1.0
    found = sum(1 for t in crits if t["id"] in matched_ids)
    return found / len(crits)


# --- feature-add (multi-file) grader ---------------------------------------

# Match fenced blocks with an explicit filename header. We accept three forms:
#
#   ```python:path/to/file.py
#   ...
#   ```
#
#   ```python
#   # FILE: path/to/file.py
#   ...
#   ```
#
#   ```python
#   # path/to/file.py
#   ...
#   ```
#
# The model is instructed to use the first form. The other two forms are
# accepted as a courtesy in case the model resists.

_FENCE_WITH_PATH_HEADER = re.compile(
    r"```(?:python|py)\s*:\s*(?P<path>[^\s`]+)\s*\n(?P<body>.*?)```",
    re.DOTALL | re.IGNORECASE,
)

_FENCE_WITH_FILE_COMMENT = re.compile(
    r"```(?:python|py)\s*\n"
    r"(?:#\s*(?:FILE|file)\s*:\s*(?P<path>[^\n]+)\n)"
    r"(?P<body>.*?)```",
    re.DOTALL | re.IGNORECASE,
)

_FENCE_WITH_BARE_PATH_COMMENT = re.compile(
    r"```(?:python|py)\s*\n"
    r"(?:#\s*(?P<path>[\w./_-]+\.py)\s*\n)"
    r"(?P<body>.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def extract_files(text: str) -> dict[str, str]:
    """Parse model output into a `{relative_path: file_body}` mapping.

    Tries the three accepted forms in order; later definitions override
    earlier ones for the same path (so the model can rewrite files).
    Returns an empty dict if nothing parses.
    """
    if not text:
        return {}
    out: dict[str, str] = {}
    for matcher in (
        _FENCE_WITH_PATH_HEADER,
        _FENCE_WITH_FILE_COMMENT,
        _FENCE_WITH_BARE_PATH_COMMENT,
    ):
        for m in matcher.finditer(text):
            path = (m.group("path") or "").strip().strip('"').strip("'")
            body = m.group("body").rstrip() + "\n"
            if not path:
                continue
            # Strip leading directory traversal as a basic safety measure --
            # the grader only writes inside `work_dir`, but we still don't
            # want a stray "../../etc/passwd" path to escape.
            path = path.replace("\\", "/").lstrip("/")
            while path.startswith("../"):
                path = path[3:]
            out[path] = body
    return out


def grade_feature_add(
    task: dict[str, Any],
    response_text: str,
    *,
    work_dir: pathlib.Path,
    pytest_timeout: float = 120.0,
) -> GradeResult:
    """Layer the model's files onto a base codebase and run acceptance tests.

    Task `grading` fields:
        codebase_dir:  path (relative to TASKS_DIR) of the unmodified source
                       tree to copy as the starting point.
        test_file:     name of the pytest file (relative to TASKS_DIR).
        weights:       composite score weights. Recognized features are:
                          passes_tests           tests passed / total
                          syntactic_validity     all written files parse
                          type_hints             ratio of public funcs annotated
                          docstring              new files have a module docstring
                          no_third_party_imports written files only import stdlib + base
    """
    started = time.time()
    grading = task.get("grading") or {}
    codebase_rel = grading.get("codebase_dir")
    test_file = grading.get("test_file")
    if not codebase_rel or not test_file:
        raise ValueError("feature_add task missing codebase_dir or test_file")

    codebase_src = TASKS_DIR / codebase_rel
    test_src = TASKS_DIR / test_file
    if not codebase_src.exists():
        raise FileNotFoundError(f"codebase not found: {codebase_src}")
    if not test_src.exists():
        raise FileNotFoundError(f"acceptance tests not found: {test_src}")

    work_dir.mkdir(parents=True, exist_ok=True)
    # Always save the raw model response for offline inspection. We do this
    # before any extraction so a parse failure leaves something to debug.
    raw_path = work_dir / "response.txt"
    raw_path.write_text(response_text or "")
    submission_dir = work_dir / "_submission"
    if submission_dir.exists():
        shutil.rmtree(submission_dir)
    shutil.copytree(codebase_src, submission_dir)

    # Drop any non-Python sidecar files that came along (e.g. ground truth).
    for stray in submission_dir.iterdir():
        if stray.is_file() and stray.suffix != ".py":
            stray.unlink()

    files = extract_files(response_text or "")
    written_files: list[pathlib.Path] = []
    rejected: list[str] = []
    for rel_path, body in files.items():
        # Path within the submission. Models tend to prefix with the task
        # codebase name (e.g. "_codebase/api.py" or "webhookd/api.py") --
        # we strip leading directory segments to get a flat layout.
        flat_name = pathlib.Path(rel_path).name
        if not flat_name.endswith(".py"):
            rejected.append(rel_path)
            continue
        target = submission_dir / flat_name
        target.write_text(body)
        written_files.append(target)

    # Save the test file *next to* the submission so its `_submission/`
    # path resolution works.
    test_dst = work_dir / pathlib.Path(test_file).name
    shutil.copy(test_src, test_dst)

    # Quality features only over what the model actually wrote. If the
    # model wrote no files at all, all quality flags are False (a model
    # that returns prose instead of code shouldn't score on quality).
    syntactic_ok     = bool(written_files)
    docstring_ok     = bool(written_files)
    type_hints_ok    = bool(written_files)
    no_thirdparty_ok = bool(written_files)
    allowed_stdlib = {
        # Stdlib modules the codebase already uses, plus the obvious ones.
        "json", "sqlite3", "time", "uuid", "hmac", "hashlib", "logging",
        "re", "os", "sys", "pathlib", "argparse", "threading", "queue",
        "collections", "dataclasses", "typing", "abc", "math", "random",
        "urllib", "ssl", "socket", "io", "http", "functools", "operator",
        "weakref", "heapq", "__future__",
        # The codebase's own modules; importing them is fine.
        "auth", "api", "config", "dispatcher", "event_filters", "http_client",
        "metrics", "models", "rate_limiter", "signing", "storage",
        "worker_pool", "admin_cli", "quotas",
    }
    quality_log: list[dict[str, Any]] = []
    for path in written_files:
        body = path.read_text()
        ok = _check_syntactic(body)
        if not ok:
            syntactic_ok = False
        # Docstrings only required on brand-new files (heuristic: the file
        # was not in the original codebase).
        is_new = not (codebase_src / path.name).exists()
        has_doc = _has_module_docstring(body, min_words=20) if is_new else True
        if not has_doc:
            docstring_ok = False
        if not _public_funcs_have_type_hints(body, min_ratio=0.6):
            type_hints_ok = False
        if not _no_third_party_imports(body, allowed_stdlib):
            no_thirdparty_ok = False
        quality_log.append({
            "file":           path.name,
            "is_new":         is_new,
            "syntactic_ok":   ok,
            "has_docstring":  has_doc,
        })

    # Run pytest only if all written files parse; otherwise short-circuit.
    if syntactic_ok and written_files:
        passed, failed, errors, total, stdout, stderr, gms = _run_pytest(
            work_dir, pathlib.Path(test_file).name, timeout=pytest_timeout,
        )
    elif not written_files:
        passed = failed = errors = total = 0
        stdout = ""
        stderr = "no files extracted from response"
        gms = 0
    else:
        passed = failed = total = 0
        errors = 1
        stdout = ""
        stderr = "syntax error in submitted file - skipped pytest"
        gms = 0

    passes_tests = passed / total if total else 0.0

    feature = {
        "passes_tests":            passes_tests,
        "syntactic_validity":      1.0 if syntactic_ok else 0.0,
        "type_hints":              1.0 if type_hints_ok else 0.0,
        "docstring":               1.0 if docstring_ok else 0.0,
        "no_third_party_imports":  1.0 if no_thirdparty_ok else 0.0,
    }
    weights = dict(grading.get("weights") or {})
    score = 0.0
    weight_total = 0.0
    for k, w in weights.items():
        if k in feature:
            score += float(w) * feature[k]
            weight_total += float(w)
    if weight_total > 0:
        score /= weight_total
    else:
        score = passes_tests

    summary_path = work_dir / "grade.json"
    summary_path.write_text(json.dumps({
        "n_files_written":     len(written_files),
        "files_written":       [p.name for p in written_files],
        "rejected_paths":      rejected,
        "syntactic_ok":        syntactic_ok,
        "type_hints_ok":       type_hints_ok,
        "docstring_ok":        docstring_ok,
        "no_thirdparty_ok":    no_thirdparty_ok,
        "pytest_passed":       passed,
        "pytest_failed":       failed,
        "pytest_errors":       errors,
        "pytest_total":        total,
        "passes_tests":        round(passes_tests, 4),
        "score":               round(score, 4),
        "quality_log":         quality_log,
        "pytest_stdout_tail":  (stdout or "")[-2000:],
        "pytest_stderr_tail":  (stderr or "")[-500:],
    }, indent=2))

    grade_ms = int((time.time() - started) * 1000)
    return GradeResult(
        task_id=task["id"],
        work_dir=work_dir,
        code_path=summary_path,
        syntactic_ok=syntactic_ok,
        has_docstring=docstring_ok,
        has_type_hints=type_hints_ok,
        no_thirdparty=no_thirdparty_ok,
        pytest_passed=passed,
        pytest_failed=failed,
        pytest_errors=errors,
        pytest_total=total,
        grade_ms=grade_ms,
        composite_score=round(score, 4),
        raw_pytest_stdout=stdout,
        raw_pytest_stderr=stderr,
    )
