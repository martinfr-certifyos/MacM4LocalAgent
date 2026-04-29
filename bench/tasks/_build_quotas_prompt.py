"""Build the quotas_feature task prompt by concatenating the base
codebase and an explicit specification for the new feature.

Run with `python -m bench.tasks._build_quotas_prompt` to write the
default variant. Pass `--variant <id>` to write one of the prompt
variants used in the SQLite-hint ablation:

    base       no extra hint (the original prompt)
    sqlite     adds an explicit "this is SQLite, not Postgres" hint
    style      adds a "mirror existing storage.py style" hint
    negative   adds explicit "do NOT use FOR UPDATE / BEGIN IMMEDIATE" rules

Each variant writes a separate JSON spec file so they can be selected
by `--task quotas_feature_<variant>` in the runner.
"""
from __future__ import annotations

import argparse
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
CODEBASE_DIR = HERE / "quotas_feature" / "_codebase"


VARIANTS: dict[str, dict[str, str]] = {
    "base": {
        "task_id":   "quotas_feature",
        "out_name":  "quotas_feature.json",
        "extra_hint": "",
    },
    "sqlite": {
        "task_id":   "quotas_feature_sqlite",
        "out_name":  "quotas_feature_sqlite.json",
        "extra_hint": (
            "   - The database is SQLite, not Postgres. SQLite does NOT support "
            "`SELECT ... FOR UPDATE` and there is no row-level locking. "
            "Atomicity comes from individual statements within a connection's "
            "implicit transaction; for a check-and-increment, prefer either a "
            "single `UPDATE ... WHERE current_count + ? <= monthly_limit` "
            "(checking `cur.rowcount` to detect rejection) or an `INSERT OR "
            "IGNORE` followed by a separate `UPDATE`. Do NOT emit `FOR UPDATE`, "
            "`LOCK IN SHARE MODE`, or any other Postgres/MySQL-specific locking "
            "syntax."
        ),
    },
    "style": {
        "task_id":   "quotas_feature_style",
        "out_name":  "quotas_feature_style.json",
        "extra_hint": (
            "   - Match the style of the existing `storage.py`: each public "
            "helper opens no explicit transaction, runs one or two "
            "`conn.execute(...)` calls, then `conn.commit()` once at the end. "
            "Do not introduce `BEGIN IMMEDIATE`, savepoints, or context "
            "managers around the connection. Look at how "
            "`storage.create_subscription` and `storage.enqueue_delivery` "
            "structure their writes and follow the same pattern."
        ),
    },
    "negative": {
        "task_id":   "quotas_feature_negative",
        "out_name":  "quotas_feature_negative.json",
        "extra_hint": (
            "   - Do NOT use any of the following constructs in your "
            "implementation: `SELECT ... FOR UPDATE`, `LOCK IN SHARE MODE`, "
            "`BEGIN IMMEDIATE`, `BEGIN EXCLUSIVE`, `SAVEPOINT`, `RELEASE`, "
            "or any explicit transaction management. The codebase relies on "
            "SQLite's per-statement atomicity within the implicit transaction "
            "that `conn.execute(...); conn.commit()` produces."
        ),
    },
}

FILE_ORDER = [
    "config.py",
    "models.py",
    "storage.py",
    "signing.py",
    "auth.py",
    "http_client.py",
    "rate_limiter.py",
    "event_filters.py",
    "metrics.py",
    "worker_pool.py",
    "dispatcher.py",
    "api.py",
    "admin_cli.py",
]

INSTRUCTIONS = """\
You are a senior staff engineer adding a small feature to a Python webhook \
delivery service called `webhookd`. The complete current source tree (13 \
modules) is included below between clearly delimited file markers. The \
modules use FLAT imports (`import storage`, not `from . import storage`) \
because the test harness places the submission on `sys.path`. Keep that \
convention.

================ FEATURE TO ADD: per-tenant monthly delivery quota ================

We need to start charging tenants for delivered events, and we want to \
stop runaway senders before the bill explodes. Add a per-tenant \
"monthly_limit" that caps how many delivery enqueues (the count of \
delivery rows actually written by `ingest_event`, NOT the count of \
incoming events) the tenant can produce in a calendar month. Once the \
limit is hit, further enqueues for that tenant return HTTP 429 with a \
clear error.

REQUIREMENTS

1. Schema: add a new SQLite table named `tenant_quotas` with columns:
       tenant_id     TEXT PRIMARY KEY
       monthly_limit INTEGER NOT NULL          -- 0 means unlimited
       current_count INTEGER NOT NULL DEFAULT 0
       period_start  INTEGER NOT NULL          -- unix sec; rolls over
   Make sure the table is created on the first call (idempotent CREATE
   TABLE IF NOT EXISTS, just like the rest of the schema in storage.py).

2. Provide three helper functions exposed as either a NEW MODULE
   `quotas.py` (preferred) OR added to `storage.py`. They MUST be
   importable as `from quotas import ...` OR `from storage import ...`.
   The grader will try both. Function signatures:

       def get_quota(conn, tenant_id) -> dict
           Returns {"tenant_id", "monthly_limit", "current_count",
                    "period_start", "remaining"}. Auto-creates a row
           with monthly_limit=0 (unlimited) if absent. Performs lazy
           rollover (see #4).

       def set_quota(conn, tenant_id, monthly_limit) -> None
           Upsert. Preserves current_count and period_start when an
           existing row is updated. Raise ValueError on negative limit.

       def try_consume_quota(conn, tenant_id, n=1) -> bool
           ATOMICALLY checks whether current_count + n <= monthly_limit
           (treating monthly_limit == 0 as unlimited) and increments
           current_count by n if so. Returns True on consume success,
           False on rejection. MUST NOT increment on rejection. Calls
           rollover internally if needed.

3. Wire the quota into `api.ingest_event`: for each subscription that
   would receive the event (i.e. matches the active+event_types
   filter), call `try_consume_quota(conn, tenant_id, n=1)`. If it
   returns False, STOP fanning out, and return HTTP 429 with body
   `{"error": "monthly_quota_exceeded", "event_id": ..., "delivery_ids":
   [...delivery_ids enqueued before the quota ran out...]}`. If the
   whole fan-out succeeds, return 202 Accepted as today.

4. Period rollover: a "month" for our purposes is 31 days (60*60*24*31
   seconds). If `now - period_start >= 31*86400` at any get_quota OR
   try_consume_quota call, RESET current_count = 0 and period_start =
   now BEFORE evaluating the rest of the function.

5. Two new admin routes on `api.py`:

       GET  /api/v1/tenants/{tid}/quota
            Returns the same dict get_quota() emits.
            403 if the authenticated tenant_id != tid.

       PUT  /api/v1/tenants/{tid}/quota
            Body: {"monthly_limit": <int>}
            Calls set_quota and then returns the updated quota dict.
            403 if mismatched. 400 if monthly_limit is missing,
            non-integer, or negative.

6. Constraints:
   - stdlib only (no new third-party deps)
   - Python 3.11+
   - thread-unsafe DB access is fine (the dispatcher uses a single
     connection per thread, see existing code)
   - keep the new code well-typed and documented in the style of the
     existing modules
{EXTRA_HINT_BLOCK}

================ RESPONSE FORMAT (REQUIRED) ================

Output ONLY the changed and new files. For each file, use a fenced \
Python code block with the filename in the language tag, like this:

```python:quotas.py
\"\"\"...module docstring...\"\"\"
def get_quota(conn, tenant_id):
    ...
```

```python:api.py
\"\"\"...the FULL contents of api.py after your edits...\"\"\"
import auth
import dispatcher
import quotas
...
```

Critical formatting rules:
  - Each fenced block must start with ```python:<filename>` (no leading \
    directory path; just the bare filename).
  - For files you EDIT, output the FULL final contents of that file. \
    Do not output a diff or a partial file -- the grader replaces the \
    base file wholesale with what you send.
  - For NEW files (e.g. `quotas.py`), output the full new file.
  - Do not include any commentary outside the fenced blocks. The grader \
    will ignore prose but you waste tokens.
  - Do not include `requirements.txt`, tests, README, or any non-Python \
    file. The acceptance test suite is hidden; you cannot affect it.

================ GRADING ================

A hidden pytest suite of ~20 tests will run against your submission \
laid on top of the codebase. Composite score = 0.80 x (tests passed / \
total tests) + 0.20 x code quality (parses, has docstrings, type hints, \
no third-party imports).

Now produce the files.

==================== BEGIN CODEBASE ====================
"""

FOOTER = """
==================== END CODEBASE ====================

Now output the changed and new Python files in the format specified at \
the top of this prompt. Remember: ```python:<filename> headers, full \
file bodies, no prose between blocks.
"""


def build_codebase_blob() -> str:
    parts: list[str] = []
    for name in FILE_ORDER:
        path = CODEBASE_DIR / name
        body = path.read_text()
        parts.append(f"\n----- FILE: {name} -----\n{body}")
    return "".join(parts)


def build_prompt(extra_hint: str) -> str:
    """Build the full prompt text for one variant.

    The base INSTRUCTIONS string contains a `{EXTRA_HINT_BLOCK}` token at
    the end of the constraints list; we substitute the hint text (or an
    empty string for the base variant). We do this with a literal token
    replacement instead of `.format()` because the prompt body contains
    other `{...}` substrings that must be preserved verbatim (route path
    placeholders, JSON examples, etc.).
    """
    sentinel = "{EXTRA_HINT_BLOCK}"
    if sentinel not in INSTRUCTIONS:
        raise RuntimeError("INSTRUCTIONS template lost its hint sentinel")
    if extra_hint:
        # Add a blank line above so the new bullet renders cleanly even
        # if the model is parsing the constraint list as a numbered list.
        body = INSTRUCTIONS.replace(sentinel, extra_hint.rstrip())
    else:
        body = INSTRUCTIONS.replace(sentinel, "").replace("\n\n6.", "\n6.")
        # The sentinel was on its own line; collapse the now-empty line
        # so the prompt doesn't have a stray blank inside the list.
        body = body.replace("\n\n================ RESPONSE FORMAT",
                            "\n================ RESPONSE FORMAT")
    return body + build_codebase_blob() + FOOTER


def write_spec(variant: str) -> pathlib.Path:
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}; pick one of {sorted(VARIANTS)}")
    cfg = VARIANTS[variant]
    prompt = build_prompt(cfg["extra_hint"])

    spec = {
        "id":          cfg["task_id"],
        "title":       f"Add per-tenant monthly delivery quotas to webhookd ({variant} variant)",
        "difficulty":  "long-context-feature-add",
        "estimated_human_minutes": 90,
        "description": (
            "The model receives the entire ~80 KB webhookd codebase and a "
            "feature specification for per-tenant monthly delivery quotas. "
            "It must produce a multi-file response (new module + edits to "
            "api.py) that satisfies a hidden 20-test acceptance suite. "
            f"Variant: {variant}."
        ),
        "deliverables": [
            "A new `quotas.py` module (or equivalent helpers in storage.py).",
            "Updated `api.py` with two new admin routes and quota enforcement in ingest_event.",
            "Each file as a separate ```python:<filename>``` fenced block.",
        ],
        "constraints": [
            "Python 3.11+. Stdlib only.",
            "Files must use flat imports (`import storage`), not relative imports.",
            "Output full file bodies, not diffs.",
        ],
        "prompt": prompt,
        "grading": {
            "kind":         "feature_add",
            "codebase_dir": "quotas_feature/_codebase",
            "test_file":    "quotas_feature/test_quotas.py",
            "weights": {
                "passes_tests":           0.80,
                "syntactic_validity":     0.05,
                "docstring":              0.05,
                "type_hints":             0.05,
                "no_third_party_imports": 0.05,
            },
        },
    }
    out_path = HERE / cfg["out_name"]
    out_path.write_text(json.dumps(spec, indent=2))
    char_count = len(prompt)
    print(f"wrote {out_path}  ({variant})")
    print(f"  prompt size: {char_count:,} chars")
    try:
        import tiktoken
        for enc_name in ("cl100k_base",):
            n = len(tiktoken.get_encoding(enc_name).encode(prompt))
            print(f"  {enc_name}: {n:,} tokens")
    except ImportError:
        print(f"  approx tokens: ~{char_count // 3.6:.0f}")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--variant", default="all",
        help="Which variant(s) to build: base | sqlite | style | negative | all",
    )
    args = parser.parse_args()
    variants = list(VARIANTS) if args.variant == "all" else [args.variant]
    for v in variants:
        write_spec(v)


if __name__ == "__main__":
    main()
