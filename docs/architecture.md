# Architecture

MacM4LocalAgent is a hybrid LLM stack tuned for Apple Silicon. It puts a
LiteLLM proxy in front of two local backends and one cloud backend, and
treats Cursor as an OpenAI-compatible client.

## Components

| Layer        | Component                          | Purpose                                                 |
| ------------ | ---------------------------------- | ------------------------------------------------------- |
| Client       | Cursor IDE                          | OpenAI-compatible client; talks to LiteLLM              |
| Proxy        | **LiteLLM** (`:4000`)               | Routing, auth, logging, cost                            |
| Routing      | `router/route_by_size.py`           | Token-size + complexity gate, picks the tier            |
| Local-fast   | **MLX server** (`:8081`)            | Fastest path; short prompts (≤16k tokens)               |
| Local-long   | **Ollama** (`:11434`)               | TurboQuant KV cache; long prompts (16k–128k tokens)     |
| Cloud        | **Anthropic Claude Sonnet 4.6**     | Long prompts, complex prompts, fallback                 |
| Storage      | SQLite (`cost/cost.db`)             | Per-call request log + A/B comparisons                  |
| Observability| FastAPI dashboard (`:4001`)         | Live stats, savings, A/B compare UI                     |
| Process mgr  | macOS `launchd`                     | Runs Ollama, MLX, LiteLLM, dashboard at login           |

## Diagram

```
                   ┌────────────────────┐
                   │  Cursor IDE / curl │
                   └─────────┬──────────┘
                             │ OpenAI API
                             ▼
                   ┌────────────────────┐
                   │  LiteLLM proxy     │
                   │  127.0.0.1:4000    │
                   └─────┬─────┬────────┘
              hybrid-auto│     │ explicit model
                         ▼     ▼
                ┌─────────────────────────┐
                │ SizeBasedRouter         │  ← async_pre_call_hook
                │ (router/route_by_size)  │     decides tier
                └──┬──────────┬────────┬──┘
                   │          │        │
       <16k tokens │  16-128k │        │ complex / >128k
                   ▼          ▼        ▼
            ┌──────────┐ ┌──────────┐ ┌─────────────┐
            │  MLX     │ │ Ollama   │ │  Anthropic  │
            │  :8081   │ │ :11434   │ │  Claude API │
            │  (4-8bit)│ │ (tq3 KV) │ │  Sonnet 4.6 │
            └──────────┘ └──────────┘ └─────────────┘
                   │          │              │
                   └─── log_success_event ───┘
                                │
                                ▼
                       ┌────────────────────┐
                       │  cost/cost.db      │
                       │  (requests,        │
                       │   comparisons)     │
                       └─────────┬──────────┘
                                 │
                                 ▼
                       ┌────────────────────┐
                       │  Dashboard :4001   │
                       │  (FastAPI + HTMX)  │
                       └────────────────────┘
```

## Why three tiers?

| Tier         | Strength                                                    | Weakness                              |
| ------------ | ----------------------------------------------------------- | ------------------------------------- |
| `local-fast` | Lowest latency on Apple Silicon (MLX)                       | No TurboQuant; small context window   |
| `local-long` | Up to 128k tokens via TurboQuant 3-bit KV (`tq3`)           | Slower per-token throughput than MLX  |
| `claude`    | Strongest reasoning + 200k context                          | Costs money, leaves your machine      |

The router picks the smallest tier that can carry the prompt — exactly the
inverse of how money works in cloud AI today.

## Filesystem layout

```
MacM4LocalAgent/
├── Makefile                 # Top-level orchestration (16 targets)
├── install.sh               # `make install` wrapper
├── pyproject.toml           # Python project + dev deps (pytest, ruff)
├── README.md
├── CHANGELOG.md
├── config/
│   ├── detected.env.example # Reference env template
│   └── litellm-config.yaml  # Rendered during install
├── scripts/                 # Idempotent install steps (00–90)
├── router/                  # SizeBasedRouter + complexity classifier
├── cost/                    # SQLite schema, ingest, CLI savings report
├── compare/                 # A/B comparator
├── dashboard/               # FastAPI + HTMX UI
├── launchd/                 # Plist templates rendered at install time
├── tests/                   # Python + shell test suites
└── docs/                    # You are here
```

## Boot sequence

1. `make detect` writes `config/detected.env` (chip / RAM / quant tier).
2. `make install` runs each `scripts/[0-9]*-*.sh` in order, idempotent.
3. `make start` loads four `launchd` plists; macOS keeps the services up.
4. The router callback streams every call into `cost/cost.db`.
5. The dashboard reads the DB live; CLI `make report` does too.

See [operations.md](operations.md) for what happens behind each `make` target.
