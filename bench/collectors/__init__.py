"""Provider-spend collectors.

Each collector pulls authoritative billing data from a provider's API for a
[window_start, window_end] interval and writes one or more rows into
`provider_spend`. The bench reporter then reconciles those rows against the
locally-instrumented `bench_runs.actual_cost` for the same window.

Collectors:
  - anthropic_admin.py : `/v1/organizations/usage_report/messages` and
                         `/v1/organizations/cost_report` (Admin API key,
                         x-api-key header).
  - cursor_admin.py    : `/teams/spend`, `/teams/filtered-usage-events`,
                         `/teams/daily-usage-data` (Basic auth API key).

Both modules expose a `fetch(window_start, window_end, **scope) -> list[dict]`
that returns rows ready for `bench.db.record_provider_spend`.
"""
