# MacM4LocalAgent

Hybrid local + Claude coding setup for Apple Silicon (M4 / M5 Max,
64-128 GB). Runs Qwen3-Coder-Next locally for short and long-context
prompts, falls back to Claude Sonnet 4.6 for the largest or most complex
requests, and tracks every dollar saved.

![status: alpha](https://img.shields.io/badge/status-alpha-orange)
![platform: macOS arm64](https://img.shields.io/badge/platform-macOS%20arm64-lightgrey)
![python: 3.11+](https://img.shields.io/badge/python-3.11+-blue)
![license: MIT](https://img.shields.io/badge/license-MIT-green)
![tests: 113](https://img.shields.io/badge/tests-113-brightgreen)

## At a glance

```mermaid
flowchart LR
    Cursor["Cursor IDE"] -->|"OpenAI /v1"| LiteLLM["LiteLLM :4000"]
    LiteLLM -->|"<16k fast"|         MLX["MLX :8081"]
    LiteLLM -->|"16k-128k long ctx"| Ollama["Ollama :11434 + TurboQuant"]
    LiteLLM -->|">128k or complex"| Claude["Anthropic claude-sonnet-4-6"]
    LiteLLM --> DB[("cost.db")]
    DB --> CLI["make report"]
    DB --> Dash["Dashboard :4001"]
```

## Quickstart

```bash
git clone <this repo> ~/MacM4LocalAgent
cd ~/MacM4LocalAgent

export ANTHROPIC_API_KEY="sk-ant-..."   # optional; only the Claude tier needs it

make detect      # scan hardware, write config/detected.env
make install     # brew + ollama + mlx + litellm + dashboard (idempotent)
make start       # launchd-load all four services (run at login)
make verify      # endpoint health + smoke matrix
make dashboard   # opens http://127.0.0.1:4001
make report      # CLI savings summary
```

Then point Cursor at the proxy:

1. Cursor → Settings (Cmd+,) → Models
2. Toggle **Override OpenAI Base URL**
3. Base URL: `http://127.0.0.1:4000/v1`
4. API Key: paste `LITELLM_MASTER_KEY` from `config/detected.env`
5. Add models: `local-fast`, `local-long`, `claude-code`, `hybrid-auto`

Full walkthrough in [docs/cursor-integration.md](docs/cursor-integration.md).

## Common tasks

| I want to…                              | Run this                                      |
| --------------------------------------- | --------------------------------------------- |
| Install everything                      | `make install`                                |
| Start / stop / restart the services     | `make start`, `make stop`, `make restart`     |
| Show ports and what's listening         | `make status`                                 |
| Health-check + smoke test               | `make verify`                                 |
| See savings (CLI, today/7d/30d/all)     | `make report`                                 |
| See savings (web)                       | `make dashboard`                              |
| Compare local vs Claude on one prompt   | `make compare PROMPT="..."`                   |
| Force a single request to Claude        | prefix with `[claude]`                        |
| Force a single request to local         | prefix with `[local]`                         |
| Run the 3-arm benchmark                 | `make bench TASK=lru_ttl_cache ATTEMPTS=3`    |
| Anchor real provider spend              | `make bench-pull-spend ARM=... START=... END=...` |
| Print bench comparison report           | `make bench-report TASK=lru_ttl_cache`        |
| Run the test suite                      | `make test`                                   |
| Reset everything (keep models)          | `make clean && make install`                  |
| Reset everything (also drop models)     | `make nuke && make install`                   |

## Documentation

> **First-time install?** Start with the
> [Cursor setup runbook](docs/RUNBOOK-cursor-setup.md) — concrete step-by-step
> instructions tied to your actual install values.

The deep-dive docs live under [`docs/`](docs/):

- [Cursor setup runbook](docs/RUNBOOK-cursor-setup.md) — first-time wiring
- [Architecture](docs/architecture.md) — components, ports, dataflow
- [Routing](docs/routing.md) — decision tree + worked examples
- [Cost model](docs/cost-model.md) — actual vs shadow vs savings
- [Operations](docs/operations.md) — launchd, logs, surgery
- [Cursor integration](docs/cursor-integration.md) — wiring + agent-mode caveat
- [Testing](docs/testing.md) — what each suite covers
- [Benchmarks](docs/benchmarks.md) — local vs Claude vs Cursor (with provider-billed reconciliation)
- [Troubleshooting](docs/troubleshooting.md) — symptom → fix
- [FAQ](docs/faq.md)
- [Contributing](docs/contributing.md)
- [Security](docs/security.md)
- [Changelog](CHANGELOG.md)

## Architecture (one-liner)

| Tier          | Backend                          | Model                       | Context  | Cost                    | Use it for                                   |
| ------------- | -------------------------------- | --------------------------- | -------- | ----------------------- | -------------------------------------------- |
| `local-fast`  | MLX server :8081                 | Qwen3-Coder-Next (8-bit)    | ≤16 k    | free                    | autocomplete, small refactors                |
| `local-long`  | Ollama :11434 (TurboQuant `tq3`) | Qwen3-Coder-Next (q8 GGUF)  | ≤128 k   | free                    | repo-scale Q&A, multi-file edits             |
| `claude-code` | Anthropic API                    | claude-sonnet-4-6           | 1 M      | $3 in / $15 out per 1 M | architecture, deep reasoning, >128 k         |
| `hybrid-auto` | LiteLLM router                   | (auto)                      | n/a      | varies                  | the default — let the router pick            |

The router is `router/route_by_size.py`. It estimates prompt tokens
(~1 token per 3.6 chars), runs `router/complexity_classifier.py`, and
rewrites `hybrid-auto` to one of the three real models. Every call ends up
in `cost/cost.db` with its actual + shadow cost.

## TurboQuant — the big-context unlock

Ollama is pinned to `OLLAMA_KV_CACHE_TYPE=tq3`. TurboQuant compresses the
attention KV cache ~4.6× with minimal quality loss, letting a 128 GB M5
Max comfortably hold 64–128 k tokens of context for an 80B model. Without
it, you'd be stuck around 16–32 k.

References: [llama.cpp #21131](https://github.com/ggml-org/llama.cpp/pull/21131),
[Ollama #15505](https://github.com/ollama/ollama/pull/15505).

If your `brew` Ollama predates the merge:

```bash
OLLAMA_FROM_SOURCE=1 make install
```

## Cost tracking

Every request is logged in `cost/cost.db` with both:

- **actual_cost** — real USD (0 for local).
- **shadow_cost** — what `(in_tok, out_tok)` would cost on Claude Sonnet 4.6.

`savings = sum(shadow) - sum(actual)`. Example `make report`:

```
=== Last 7 days ===
Requests:       1,243
  by tier:      local-fast 980 / local-long 138 / claude 125
Tokens in/out:  8,402,011 / 1,910,884
Actual spend:   $11.42
Shadow spend:   $54.18
Savings:        $42.76 (78.9%)
```

The full schema and aggregation rules live in
[docs/cost-model.md](docs/cost-model.md).

## Screenshots

> Add screenshots to `docs/img/` and link them here once captured. Suggested
> shots: dashboard home, compare detail, `make report` terminal output.

```
docs/img/dashboard-home.png         # http://127.0.0.1:4001
docs/img/dashboard-compare.png      # http://127.0.0.1:4001/compare/<id>
docs/img/cli-report.png             # `make report`
```

## Caveats

- **Cursor Agent mode** ignores custom OpenAI keys today. Ask and Plan
  modes work fully — see [docs/cursor-integration.md](docs/cursor-integration.md).
- **MLX has no TurboQuant yet** — the router caps `local-fast` at 16 k
  context.
- **Model availability** — if `qwen3-coder-next:80b-q8` isn't published, the
  installer falls back to `qwen3-coder-next:latest` then `qwen3-coder:30b`
  and updates `config/detected.env`.
- **Claude model id** — LiteLLM is wired for `claude-sonnet-4-6`. If
  Anthropic returns 404, switch the line in `config/litellm-config.yaml` to
  `claude-sonnet-4-5` and re-run `make install`.

## Layout

```
Makefile                # entrypoint (16 targets)
install.sh              # wrapper for `make install`
pyproject.toml          # python project + dev deps
scripts/                # 00-detect, 10-brew, 20-ollama, 30-mlx, 40-litellm,
                        # 50-cursor, 60-dashboard, 90-verify
config/                 # litellm-config.yaml, detected.env
router/                 # route_by_size.py, complexity_classifier.py
cost/                   # schema.sql, ingest.py, savings.py
dashboard/              # FastAPI + HTMX on :4001
compare/                # ab.py (A/B local vs claude)
launchd/                # plists for ollama, mlx, litellm, dashboard
tests/                  # python + shell test suites
docs/                   # deep-dive documentation
.cursor/rules/          # hybrid-routing.mdc (auto-installed)
```

## What this does NOT do

- No Docker (native launchd is faster on Apple Silicon).
- No cloud telemetry; all logs stay on-device in `cost.db`.
- No fine-tuning.

## License

MIT. See [LICENSE](LICENSE) (add one before publishing).
