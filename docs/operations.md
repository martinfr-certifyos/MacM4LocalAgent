# Operations

Day-2 operations: starting/stopping services, reading logs, doing surgery.

## Make targets

| Target            | What it does                                                           |
| ----------------- | ---------------------------------------------------------------------- |
| `make help`       | List every target with a one-line description                          |
| `make detect`     | Re-scan hardware → rewrites `config/detected.env`                      |
| `make install`    | Run all `scripts/[0-9]*-*.sh` in order. Idempotent.                    |
| `make start`      | Load all four `launchd` plists                                         |
| `make stop`       | Unload all four `launchd` plists                                       |
| `make restart`    | `stop` then `start`                                                    |
| `make status`     | Show which `launchd` jobs are loaded and which ports are alive         |
| `make verify`     | Health-probe every endpoint + run a smoke matrix                       |
| `make report`     | Pretty CLI savings report (today / 7d / 30d / all)                     |
| `make compare`    | `make compare PROMPT="..."` runs an A/B and prints the diff            |
| `make dashboard`  | Open `http://127.0.0.1:4001` in your browser                           |
| `make test`       | Run the full Python + shell suite                                      |
| `make test-py`    | Only the Python suite                                                  |
| `make test-sh`    | Only the shell suites                                                  |
| `make lint`       | `ruff check` + `bash -n` on every script                               |
| `make clean`      | `stop` + remove `.venvs/`. Keeps models and `cost.db`.                 |
| `make nuke`       | `clean` + `ollama rm` every model + delete `cost.db`. Last resort.     |

## launchd plists

Four background services live in `~/Library/LaunchAgents/com.local.*.plist`:

| Plist                        | Service           | Port  | Logs                                |
| ---------------------------- | ----------------- | ----- | ----------------------------------- |
| `com.local.ollama.plist`     | Ollama            | 11434 | `.logs/ollama.{out,err}`            |
| `com.local.mlx.plist`        | `mlx_lm.server`   | 8081  | `.logs/mlx.{out,err}`               |
| `com.local.litellm.plist`    | LiteLLM proxy     | 4000  | `.logs/litellm.{out,err}`           |
| `com.local.dashboard.plist`  | FastAPI dashboard | 4001  | `.logs/dashboard.{out,err}`         |

The repo-relative versions in `launchd/` are templates with `@@REPO_ROOT@@`
placeholders. `scripts/60-dashboard.sh` renders them into `.rendered.plist`
files; `make start` copies the rendered versions into LaunchAgents.

### Reading logs

```bash
tail -f .logs/litellm.err            # while routing requests
log show --predicate 'process == "litellm"' --info --last 30m   # macOS unified log
```

### Talking to launchd directly

```bash
launchctl list | grep com.local       # what's loaded?
launchctl unload ~/Library/LaunchAgents/com.local.ollama.plist
launchctl load -w  ~/Library/LaunchAgents/com.local.ollama.plist
launchctl print gui/$(id -u)/com.local.ollama   # detailed status
```

`make stop`/`start` does this for every service, but the granular commands
help when only one is misbehaving.

## Environment variables that matter

Set with `launchctl setenv KEY value` so child processes inherit them.

| Key                       | Effect                                            |
| ------------------------- | ------------------------------------------------- |
| `OLLAMA_KV_CACHE_TYPE`    | `tq3` enables TurboQuant 3-bit KV cache           |
| `OLLAMA_FLASH_ATTENTION`  | `1` for faster attention on Apple Silicon         |
| `OLLAMA_NUM_PARALLEL`     | concurrent requests per model (default 2)         |
| `OLLAMA_HOST`             | `127.0.0.1:11434`                                 |
| `ANTHROPIC_API_KEY`       | required for the `claude-code` tier               |
| `LITELLM_MASTER_KEY`      | random per-install secret; Cursor uses this       |

`scripts/20-ollama.sh` writes the Ollama-related ones; you set
`ANTHROPIC_API_KEY` yourself (export it in your shell, then re-run
`make start`).

## Verifying a healthy install

```bash
make status     # ports
make verify     # endpoint probes + smoke matrix
make report     # cost report (will be empty before any traffic)
```

If `make verify` is unhappy:

1. `tail -n 200 .logs/<service>.err` — most failures are model download or
   missing env vars.
2. `make stop && make start` — settles ~95% of "weird state" issues.
3. `make clean && make install` — full reset of the venvs (keeps models and
   data).
4. `make nuke && make install` — burn it down. Models get re-downloaded.

## Common surgical procedures

### Re-pull a different Ollama model

```bash
echo 'OLLAMA_TAG=qwen3-coder:30b' >> config/detected.env
make stop && bash scripts/20-ollama.sh && make start
```

### Bump the Claude pricing

Edit the two constants in `cost/ingest.py` *and* the `input_cost_per_token` /
`output_cost_per_token` in `config/litellm-config.yaml`. Both must agree.

### Move the routing thresholds

Edit `ROUTE_FAST_MAX` / `ROUTE_LONG_MAX` in `config/detected.env`, then
`make restart`.

### Wipe history but keep models

```bash
rm cost/cost.db
make restart
```

The schema is recreated on the next request.
