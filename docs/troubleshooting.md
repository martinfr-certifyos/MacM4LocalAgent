# Troubleshooting

Symptoms first; root causes and fixes underneath.

## Install fails partway through

**Symptom:** `make install` exits non-zero on one of `scripts/[0-9]*`.

**Diagnose:**
```bash
ls config/detected.env || echo "detect step failed"
which ollama uv jq sqlite3
```

**Fix:** install scripts are idempotent. Run the failing one in isolation,
read its output, then resume:
```bash
bash -x scripts/20-ollama.sh    # or whichever step blew up
make install                    # picks up where it left off
```

## `make start` says "Missing rendered plist"

**Symptom:** `Missing /path/com.local.dashboard.rendered.plist - run \`make install\` first`.

**Cause:** the renderer in `scripts/60-dashboard.sh` didn't run, usually
because an earlier step failed.

**Fix:**
```bash
bash scripts/60-dashboard.sh
make start
```

## Cursor returns 401 / "Invalid API key"

**Cause:** Cursor's API key field doesn't match `LITELLM_MASTER_KEY` in
`config/detected.env`.

**Fix:**
```bash
grep LITELLM_MASTER_KEY config/detected.env
```
Paste the value into Cursor → Settings → Models → \[your provider\] → API key.

## Cursor returns 404 model not found

**Cause:** `hybrid-auto` (or whichever model) isn't registered in LiteLLM.

**Fix:**
```bash
curl -s http://127.0.0.1:4000/models \
  -H "Authorization: Bearer $(grep MASTER_KEY config/detected.env | cut -d= -f2)" | jq
```

If `hybrid-auto` isn't in the list, the proxy started before the config
finished rendering. `make stop && make start` fixes it 99% of the time.

## Ollama starts, MLX doesn't

**Symptom:** `make status` shows port 11434 alive but 8081 dead.

**Diagnose:** `tail -n 100 .logs/mlx.err`. The most common cause is the MLX
quantized model didn't finish downloading.

**Fix:**
```bash
source .venvs/mlx/bin/activate
python -c "from mlx_lm import load; load('mlx-community/Qwen3-Coder-Next-80B-mlx-4bit')"
make restart
```

## Long prompts are extremely slow on local-long

**Cause:** TurboQuant KV cache compression isn't actually enabled. Verify:
```bash
launchctl getenv OLLAMA_KV_CACHE_TYPE     # should be tq3
```

If it's empty, run `bash scripts/20-ollama.sh` and `make restart`. Some
older Ollama builds ignore the env var; you can rebuild from source with
`OLLAMA_FROM_SOURCE=1 make install`.

## Claude tier returns 401 / 403

**Cause:** `ANTHROPIC_API_KEY` is missing.

**Fix:**
```bash
launchctl setenv ANTHROPIC_API_KEY sk-ant-...
make restart
```

Add the same `export ANTHROPIC_API_KEY=...` to your shell rc so it survives
reboots.

## Dashboard shows 0 requests but you've been using Cursor

**Cause:** Cursor isn't pointed at the LiteLLM proxy, or the success
callback is silently throwing.

**Fix:**
```bash
sqlite3 cost/cost.db "SELECT COUNT(*) FROM requests;"
tail -n 100 .logs/litellm.err | grep -i "router"
```

If the count is 0 but Cursor responds, the request bypassed the proxy. Check
the Base URL.

## Pytest fails with `cannot use 'tuple' as a dict key`

**Cause:** Python 3.14 + Jinja2 has an upstream cache bug. `dashboard/app.py`
already disables the cache (`templates.env.cache = None`); if you removed
that line, put it back.

## Pytest fails on a route with `'dict' object has no attribute 'split'`

**Cause:** Older code used `templates.TemplateResponse(name, context)`.
Starlette 1.0 changed the signature to
`TemplateResponse(request, name, context)`. All routes in `dashboard/app.py`
must pass `request` as the first arg.

## `cost.db` keeps growing

**Cause:** No retention is configured by default. The DB is small (a few KB
per request) but if you want to prune:

```sql
DELETE FROM requests WHERE ts < strftime('%s', 'now') - 60*60*24*90;
VACUUM;
```

…drops anything older than 90 days. Cron it if you'd like.

## Models pane in Cursor doesn't show `hybrid-auto`

Cursor caches the discovered model list. After registering the provider:
1. Toggle the provider off and back on.
2. Or restart Cursor.

## `make nuke` complaining about disk usage

`make nuke` deletes Ollama models which can be 30–80 GB. Make sure you have
free space — re-pulling everything takes 20+ minutes on a slow connection.

## When in doubt

```bash
make status     # ports
make verify     # endpoint probes + smoke matrix
make test       # are the unit tests still green?
```

If `make test` is green but real Cursor calls fail, the issue is in the
runtime config (env vars, plists) — not the code.
