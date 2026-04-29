"""Build the bug_hunt_codebase prompt by concatenating all source files.

Run with `python -m bench.tasks._build_bug_hunt_prompt` from the repo root.
Writes the resulting `bug_hunt_codebase.json` task spec next to itself.

We do this at build time rather than at runtime so the task spec is a
single self-contained artifact that the runners and tests can use
without re-reading the codebase fixtures.
"""
from __future__ import annotations

import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
CODEBASE_DIR = HERE / "bug_hunt_codebase"
OUT_PATH = HERE / "bug_hunt_codebase.json"

# Order matters: present the codebase top-down so the model sees the
# entry point + data model first and the helpers afterwards.
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
You are a senior staff engineer doing a careful, focused code review of a small \
Python service called `webhookd` -- a webhook delivery system that stores \
subscriptions, accepts events, signs payloads, and delivers them with retries \
and a dead-letter queue.

The full source tree (13 modules, ~80 KB of Python) is included below between \
clearly delimited file markers. Treat this as a real production codebase: it is \
not toy code, and most of it is correct. Your job is to find the small set of \
real bugs hiding in it.

Definition of a bug for this exercise:
  - A correctness defect that would cause incorrect behaviour, data loss, \
infinite loops, or security holes in normal operation.
  - Severity ranges from "critical" (silent data loss, cross-tenant data \
leak, infinite redelivery) down to "medium" (e.g. wrong eviction order, \
non-cumulative metrics).
  - Style nits, missing tests, or merely "could be better" observations are \
NOT bugs and will count against you if reported.

For each bug you find, return:
  - file:        the source filename, e.g. "storage.py"
  - function:    the function or method name where the bug lives, e.g. \
"move_to_dead_letter" or "_Histogram.observe"
  - severity:    "critical" | "high" | "medium" | "low"
  - category:    "correctness" | "security" | "concurrency" | "performance"
  - summary:     one to three sentences describing the bug and its impact

Output format -- this is required:

Respond with EXACTLY one JSON object inside a single ```json ...``` fenced \
code block. No prose outside the block. The object must have a single key \
`bugs` whose value is an array of bug objects in the schema above. Example:

```json
{
  "bugs": [
    {
      "file": "example.py",
      "function": "do_thing",
      "severity": "high",
      "category": "correctness",
      "summary": "Loop variable is reassigned inside the loop, which makes \
the iteration skip every other element."
    }
  ]
}
```

Do NOT include any commentary, explanation, or markdown outside the JSON \
block. Do NOT include any code fixes or patches. Just the JSON.

Quality criteria the grader will apply:
  - Recall: how many of the real bugs you found.
  - Precision: how few false positives you raised. Reporting things that \
aren't actually bugs reduces your score.
  - Critical-bug recall is weighted higher than the rest.

Take your time and look at every file. Now: find the bugs.

==================== BEGIN CODEBASE ====================
"""

FOOTER = """
==================== END CODEBASE ====================

Now produce the JSON object as specified above. Remember: ONLY the \
```json ...``` fenced block. No prose.
"""


def build_codebase_blob() -> str:
    parts: list[str] = []
    for name in FILE_ORDER:
        path = CODEBASE_DIR / name
        body = path.read_text()
        parts.append(f"\n----- FILE: {name} -----\n{body}")
    return "".join(parts)


def main() -> None:
    codebase = build_codebase_blob()
    prompt = INSTRUCTIONS + codebase + FOOTER

    spec = {
        "id": "bug_hunt_codebase",
        "title": "Find correctness and security bugs in a small webhook delivery service",
        "difficulty": "long-context-hard",
        "estimated_human_minutes": 90,
        "description": (
            "The model receives a complete ~80 KB Python codebase (13 modules) "
            "implementing a webhook delivery service. A small number of real "
            "correctness and security bugs are seeded into it. The model must "
            "identify each bug as a JSON object with file/function/severity/"
            "category/summary, and is graded on precision and recall against a "
            "hidden ground-truth list."
        ),
        "deliverables": [
            "A single ```json``` fenced block containing {\"bugs\": [...]}.",
            "Each bug object has file, function, severity, category, summary.",
        ],
        "constraints": [
            "No prose outside the JSON block.",
            "No code fixes -- only the bug list.",
            "Do not report style nits, missing tests, or 'could be better' issues.",
        ],
        "prompt": prompt,
        "grading": {
            "kind": "bug_hunt",
            "ground_truth": "bug_hunt_codebase/_ground_truth.json",
            "weights": {
                "f1":             0.55,
                "found_critical": 0.25,
                "precision":      0.10,
                "valid_json":     0.10,
            },
        },
    }
    OUT_PATH.write_text(json.dumps(spec, indent=2))
    char_count = len(prompt)
    approx_tokens = int(char_count / 3.6)
    print(f"wrote {OUT_PATH}")
    print(f"prompt size: {char_count:,} chars, ~{approx_tokens:,} tokens")


if __name__ == "__main__":
    main()
