# Cost model

We track two numbers per request:

- **`actual_cost`** — what this call really cost the user. Local calls = `0.0`.
  Claude calls use the per-token rates wired into `litellm-config.yaml`.
- **`shadow_cost`** — what the same call *would have* cost on Claude Sonnet 4.6,
  irrespective of where it actually ran. This is the counterfactual.

`savings = shadow - actual` per request, summed for any time window.

## Pricing constants

Defined once in `cost/ingest.py` and mirrored in the router callback so the
CLI and the dashboard agree:

```python
CLAUDE_INPUT_USD_PER_1M  = 3.0
CLAUDE_OUTPUT_USD_PER_1M = 15.0
```

For a request with `in_tok` input tokens and `out_tok` output tokens:

```
shadow_cost = (in_tok  / 1_000_000) * 3.0
            + (out_tok / 1_000_000) * 15.0
```

For a Claude call, `actual_cost == shadow_cost`. For a local call,
`actual_cost == 0` and `shadow_cost` measures the dollars saved.

## Schema

`cost/schema.sql` defines two tables:

```sql
CREATE TABLE requests (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            INTEGER NOT NULL,                 -- unix seconds
  model         TEXT    NOT NULL,                 -- e.g. ollama/qwen3-coder:30b
  tier          TEXT    NOT NULL,                 -- local-fast | local-long | claude
  input_tok     INTEGER NOT NULL DEFAULT 0,
  output_tok    INTEGER NOT NULL DEFAULT 0,
  actual_cost   REAL    NOT NULL DEFAULT 0,
  shadow_cost   REAL    NOT NULL DEFAULT 0,
  latency_ms    INTEGER NOT NULL DEFAULT 0,
  route_reason  TEXT
);

CREATE TABLE comparisons (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            INTEGER NOT NULL,
  prompt        TEXT,
  local_model   TEXT,
  claude_model  TEXT,
  local_output  TEXT,
  claude_output TEXT,
  local_in_tok  INTEGER, local_out_tok  INTEGER,
  claude_in_tok INTEGER, claude_out_tok INTEGER,
  local_cost    REAL, claude_cost REAL,
  local_ms      INTEGER, claude_ms INTEGER,
  judge_score   REAL
);
```

Schema is created on first connect and is idempotent — re-running
`make install` doesn't drop your data.

## Aggregation

`cost/savings.py` exposes:

- `summarize(days: int | None) -> dict` — totals + per-tier breakdown.
- `main(argv)` — CLI entry. With no args, prints today / 7d / 30d / all-time.
  With one numeric arg, prints that window. With `--json`, prints JSON.

The shape of `summarize` output:

```json
{
  "window_days": 7,
  "from_ts": 1714099200,
  "total_requests": 142,
  "total_input_tokens": 412310,
  "total_output_tokens": 89221,
  "actual_spend_usd": 1.97,
  "shadow_spend_usd": 12.80,
  "savings_usd": 10.83,
  "savings_pct": 84.6,
  "by_tier": {
    "local-fast":  { "n": 110, "input_tokens": 30_011, "output_tokens": 9_412,
                     "actual_usd": 0.0, "shadow_usd": 0.23, "avg_latency_ms": 95 },
    "local-long":  { ... },
    "claude":      { ... }
  }
}
```

## How LiteLLM hands data to us

The router callback `log_success_event(kwargs, response_obj, start_time, end_time)`
receives:

- `kwargs["model"]` — the **rewritten** model name (post-routing).
- `response_obj.usage` — Pydantic model with `prompt_tokens`/`completion_tokens`
  (or a plain dict on some LiteLLM versions; we handle both).
- `start_time`, `end_time` — `time.time()` floats from LiteLLM.

We compute `latency_ms = int((end - start) * 1000)`, derive `tier` from the
model string, and call `cost.ingest.record_request(...)`. Test:
`tests/test_router.py::test_log_success_event_records_*`.

## A/B comparison cost

`compare/ab.py::run(prompt)` always sends the same prompt to **both**
`local-long` and `claude-code` via the LiteLLM proxy, then writes a single row
to `comparisons`. The row carries:

- `local_cost = 0.0` (always),
- `claude_cost = shadow_cost(in, out)`,
- `judge_score` — a 0..1 similarity heuristic based on length ratio and code
  fence count. It's intentionally crude; treat it as a sniff test, not a
  benchmark.

## Mental model: when does this break?

- **Local model is wrong but free.** `savings_usd` says you saved money, but
  the A/B comparator might score it 0.4. Always sanity-check on real prompts.
- **Latency vs. quality.** `local-long` can be 5–20× slower than `local-fast`
  on the same prompt. Cost stays $0; throughput suffers. The dashboard's
  recent-request table shows `latency_ms` so you can spot pathological cases.
- **Token estimation drift.** `chars / 3.6` underestimates non-English text.
  If you do a lot of multilingual work, swap in `tiktoken` (see
  [routing.md](routing.md)).
