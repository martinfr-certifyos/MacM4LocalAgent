# Security model

This is a single-user, on-machine setup. The threat model is small but
not zero.

## What stays on-device

- Prompts and responses for `local-fast` and `local-long`.
- The cost DB (`cost/cost.db`).
- A/B comparison transcripts (`comparisons` table).
- All log files under `.logs/`.
- LiteLLM config, including the master key.

## What leaves the device

- Prompts and responses for the `claude` tier (sent to Anthropic's API).
- That's it.

The router never proxies to any other cloud, and the dashboard binds
strictly to `127.0.0.1`.

## Secrets

| Secret                | Where                                               |
| --------------------- | --------------------------------------------------- |
| `LITELLM_MASTER_KEY`  | `config/detected.env`, generated at `make detect`   |
| `ANTHROPIC_API_KEY`   | `launchctl setenv` + your shell rc                  |

`.gitignore` excludes `config/detected.env`, `cost/cost.db`, `.logs/`, and
`.venvs/`. Don't add them to commits.

## Network exposure

All four services bind to `127.0.0.1`:

| Service     | Port  |
| ----------- | ----- |
| LiteLLM     | 4000  |
| MLX         | 8081  |
| Ollama      | 11434 |
| Dashboard   | 4001  |

If you want LAN access (e.g. another machine using your local LLMs):

1. Edit the `launchd` plist to bind `0.0.0.0`.
2. Add an authentication layer — at minimum, a reverse proxy with basic
   auth. The LiteLLM master key alone is fine for `LITELLM`, but Ollama
   and MLX have **no auth** by default.
3. Open the relevant port in macOS firewall (`System Settings → Network →
   Firewall → Options`).

This is explicitly out of scope for the default install. If you do it,
treat the host as semi-trusted.

## File permissions

`scripts/00-detect.sh` writes `config/detected.env` with default umask.
The master key in that file is per-install random; rotating it is just
re-running detect:

```bash
make detect
make restart
```

## Process isolation

LiteLLM runs as your user under `launchctl`. It can't access another
user's files. It *can* read your home directory like any process you
launch — including SSH keys. If you don't trust the LiteLLM proxy code,
sandbox it (e.g. a separate user account or a container). For most
single-developer setups this is overkill.

## Anthropic API key handling

LiteLLM reads `ANTHROPIC_API_KEY` from the process environment. The router
callback never logs it. The cost DB stores token counts and dollar
amounts, never headers or keys.

If you suspect a leak:

```bash
grep -r "sk-ant-" cost/ .logs/ config/
```

…should return zero matches.

## Audit trail

Every request is logged with `ts`, `model`, `tier`, `input_tok`,
`output_tok`, `actual_cost`, `latency_ms`, and `route_reason`. Prompts and
responses are *not* stored in the `requests` table — only A/B
comparisons keep them, and only because the comparator explicitly needs
both outputs side by side.

If you want full prompt logging, add `prompt_hash TEXT` to the `requests`
schema and stash a SHA-256 of the messages. Don't store plaintext unless
you've thought about backup encryption.

## Reporting a vulnerability

This is a personal-use template, so there's no formal disclosure process.
File an issue on the repo, mark it `security`, and the maintainer will
respond.
