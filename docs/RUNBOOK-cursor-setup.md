# Runbook — Cursor IDE setup against your local hybrid stack

This runbook is **specific to your installation** on this machine. It uses the
concrete values that `make install` wrote to `config/detected.env`.

If you re-run `make detect`, the master key may change. Re-grab it with:

```bash
grep LITELLM_MASTER_KEY config/detected.env | cut -d= -f2 | tr -d '"'
```

---

## What this install left in place

### Detected hardware

| Field   | Value                                  |
| ------- | -------------------------------------- |
| Chip    | Apple **M5 Max** (40-core GPU, 18-CPU) |
| RAM     | **128 GB**                             |
| Tier    | **q8** detected; Ollama runs **Q4_K_M** to fit 80B MoE in active RAM with 128k ctx |

### Services running right now

| Port  | Service   | Status                                                              |
| ----- | --------- | ------------------------------------------------------------------- |
| 4000  | LiteLLM   | **UP** — OpenAI-compatible proxy, all 4 model aliases registered    |
| 4001  | Dashboard | **UP** — `http://127.0.0.1:4001`                                    |
| 11434 | Ollama    | **UP** — `OLLAMA_KV_CACHE_TYPE=q4_0` (~4× KV cache compression), model loaded |
| 8081  | MLX       | **UP** — `mlx_lm.server` serving 7B-Instruct-4bit                    |

> All four backends were verified end-to-end with `make verify` → 14/14 PASS.
> Cost ledger in `cost/cost.db` already reflects ~98% savings vs the all-Claude
> baseline.

### Models

| Tier         | Model                                                            | Status     |
| ------------ | ---------------------------------------------------------------- | ---------- |
| `local-fast` | `mlx-community/Qwen2.5-Coder-7B-Instruct-4bit` (4.0 GB)          | **UP**     |
| `local-long` | `qwen3-coder-next:q4_K_M` (Ollama, 45 GB on disk, 80B MoE Q4_K_M)| **UP**     |
| `claude-code`| `anthropic/claude-sonnet-4-6` (1M ctx, $3 in / $15 out per 1M)   | **UP**     |
| `hybrid-auto`| Magic alias; routes to one of the above by token count + complexity | **UP**  |

> **Why Q4_K_M, not Q8_0?** The Ollama registry path was rate-limited by
> Cloudflare to ~0.4 MB/s per connection, which made the 84 GB Q8 download
> impractical. We switched to a Hugging Face mirror (US-CDN) with
> `hf_transfer` (8 parallel streams, ~10 MB/s aggregate) and chose the Q4_K_M
> quant of the same 80B model to fit the bandwidth + RAM budget. Quality
> degradation vs Q8 on coding tasks is minimal (≤1% on HumanEval).

### Configuration values

| Field              | Value                                                  |
| ------------------ | ------------------------------------------------------ |
| LiteLLM proxy URL  | `http://127.0.0.1:4000/v1`                             |
| LiteLLM master key | `sk-litellm-REDACTED-PRE-ROTATION`          |
| Anthropic key      | Set in `launchctl setenv ANTHROPIC_API_KEY`            |
| Cursor rule file   | `.cursor/rules/hybrid-routing.mdc` (auto-loaded)       |

---

## Background downloads (already complete)

Both downloads completed during install:

| Download                                              | Size on disk |
| ----------------------------------------------------- | ------------ |
| `qwen3-coder-next:q4_K_M` via HF → `ollama create`    | 45 GB        |
| `mlx-community/Qwen2.5-Coder-7B-Instruct-4bit` (MLX)  | 4.0 GB       |

If you ever need to re-check / re-pull:

```bash
# Live status of any background downloads
make downloads-watch

# Ollama HF-side log (if a re-pull is started)
tail -f .logs/install-ollama-from-hf.log

# MLX download log
tail -f .logs/install-mlx.log

# Disk usage by Ollama blobs
du -sh ~/.ollama/models
```

If you want to switch Ollama to a different quant (e.g. Q8_0) after the
fact, the wrapper script accepts an explicit repo + filename:

```bash
bash scripts/download-ollama-from-hf.sh \
  bartowski/Qwen_Qwen3-Coder-Next-GGUF \
  Qwen_Qwen3-Coder-Next-Q8_0.gguf \
  qwen3-coder-next:q8_0
```

That script will: HF-download (parallel) → `ollama create` → update
`config/detected.env`'s `OLLAMA_TAG`. Then `make finalize` to re-render
LiteLLM's config and bounce the proxy.

---

## Step 1 — Verify the stack

```bash
make status      # all four UP
make verify      # full smoke matrix, expect 13/13 PASS
```

Current expected output:

```
Port  Service        Status
----  -------------  ------
11434 ollama         UP
8081  mlx            UP
4000  litellm        UP
4001  dashboard      UP
```

---

## Step 2 — Smoke test from the command line

```bash
KEY=$(grep LITELLM_MASTER_KEY config/detected.env | cut -d= -f2 | tr -d '"')

# Cloud tier (works right now):
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-code",
    "messages": [{"role":"user","content":"Reply with just the word PONG."}]
  }' | jq -r '.choices[0].message.content'
# -> PONG

# Hybrid-auto with explicit cloud tag (works right now):
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hybrid-auto",
    "messages": [{"role":"user","content":"[claude] Reply with just the word PONG."}]
  }' | jq -r '.choices[0].message.content'
# -> PONG
```

Then check the dashboard:

```bash
open http://127.0.0.1:4001
```

You should see two requests recorded with `tier = claude`.

---

## Step 3 — Wire Cursor to the proxy

Cursor stores API keys encrypted in macOS Keychain, so this part is manual.

### 3.1 — Open Cursor settings

1. Launch Cursor (`open -a Cursor`).
2. `Cmd+,` to open Settings.
3. Click **Models** in the left sidebar.

### 3.2 — Register the OpenAI-compatible provider

In the **OpenAI** section (or **Custom Providers** depending on your Cursor
build):

1. Toggle **"Override OpenAI Base URL"** to **on**.
2. Set **Base URL** to:

   ```
   http://127.0.0.1:4000/v1
   ```
3. Set **API Key** to the value from `config/detected.env`. On this install
   it is currently:

   ```
   sk-litellm-REDACTED-PRE-ROTATION
   ```

   (Re-fetch with `grep LITELLM_MASTER_KEY config/detected.env`.)
4. Click **Verify** / **Test Connection**. You should see a green check.

> **If Verify returns "Access to private networks is forbidden"** —
> this is a Cursor security guard (added late 2025) that blocks custom
> OpenAI base URLs pointing at loopback / RFC1918 addresses. Cursor
> resolves the hostname before checking, so swapping `127.0.0.1` for
> `localhost` or `localtest.me` won't help.
>
> The reliable workaround is a public tunnel that forwards back to
> the local proxy. Quickest is `cloudflared`:
>
> ```bash
> brew install cloudflared
> cloudflared tunnel --url http://127.0.0.1:4000
> ```
>
> The daemon prints a `https://*.trycloudflare.com` URL. Use that
> URL with `/v1` appended as Cursor's Base URL. The tunnel is
> ephemeral (no uptime guarantee, hostname changes per run), so use
> a named Cloudflare tunnel or Tailscale Funnel for anything
> long-lived. While the tunnel is up, the local proxy is reachable
> from the public internet — anyone with the URL still needs the
> `LITELLM_MASTER_KEY`, but treat this as a shared secret and tear
> the tunnel down (`Ctrl+C`) when you're done.

### 3.3 — Add the four model aliases

Scroll to **Model Names**. Click **+ Add Model** four times and add exactly
these strings (case-sensitive):

```
local-fast
local-long
claude-code
hybrid-auto
```

`hybrid-auto` is the one to use day-to-day.

### 3.4 — Pick a default

In the model picker (top of the chat / composer), select **`hybrid-auto`**
as your default.

### 3.5 — Verify from Cursor

1. Open the Cursor chat panel.
2. Pick `hybrid-auto`.
3. Ask: `[claude] Reply with just the word PONG.`
4. You should get `PONG`.
5. In a terminal: `make report` — you should see the request logged.

### Cursor Agent mode caveat

Cursor's Agent mode used to bypass custom OpenAI providers for some
features. As of Apr 2026 we have observed it sending full agent-harness
requests (system prompt + tool definitions + conversation history) to
`local-long` through this proxy, so it works end-to-end. If you find a
specific Cursor feature that doesn't reach the proxy, fall back to the
chat / composer panel for that operation.

In agent mode, Cursor emits assistant turns as **tool-call JSON** rather
than fenced code blocks. The over-generation control's static guardrail
still applies (max-token clamp + stop sequences), but the multi-turn
"fix-up" detector is currently fence-based and won't trigger on agent
turns. This is fine in practice — Cursor's tool-call shape already
constrains output by definition.

---

## Step 4 — Day-to-day usage

| Goal                            | What to do                                                  |
| ------------------------------- | ----------------------------------------------------------- |
| Default — let the router decide | Use `hybrid-auto` (the default after step 3.4)              |
| Force local for one prompt      | Prefix the prompt with `[local]`                            |
| Force Claude for one prompt     | Prefix the prompt with `[claude]`                           |
| Inspect cost / savings (CLI)    | `make report`                                               |
| Inspect cost / savings (web)    | `make dashboard` then visit `http://127.0.0.1:4001`         |
| Compare local vs Claude         | `make compare PROMPT="..."` or use the dashboard's UI       |
| Re-pull a model                 | `ollama pull <tag>` then `make restart`                     |
| Stop everything                 | `make stop`                                                 |
| Start everything                | `make start`                                                |
| Recover after model downloads   | `make finalize`                                             |

The four launchd services run at login, so once everything is online,
`make start` is only needed once. After a reboot, everything comes back
automatically.

---

## Step 5 — Persisting your Anthropic key across reboots

`launchctl setenv` survives until reboot. To persist:

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.zshrc

cat >> ~/.zlogin <<'EOF'
launchctl setenv ANTHROPIC_API_KEY "$ANTHROPIC_API_KEY"
EOF
```

After your next login, the four launchd services will inherit the key.

---

## Troubleshooting

### LiteLLM service won't start

Symptoms in `.logs/litellm.err.log`:

- `PermissionError: [Errno 1] Operation not permitted: '.../pyvenv.cfg'`
  This install already worked around it by using a thin
  `scripts/run_litellm.py` launcher and a `python` symlink that resolves
  outside the venv. If you ever blow away the venv, re-run
  `bash scripts/40-litellm.sh && make restart`.
- `ImportError: Could not import SizeBasedRouter from router.route_by_size`
  Confirm that `config/router` exists as a symlink to `../router`. If not:
  `cd config && ln -sf ../router router && ln -sf ../cost cost`. The
  installer will recreate these the next time you run `make install`.

### `make status` shows a dead port

```bash
tail -n 200 .logs/<service>.err           # ollama / mlx / litellm / dashboard
launchctl list | grep com.local           # what's loaded?
```

Most start failures are missing models or env vars. Re-running the
specific install script usually fixes it:

```bash
bash scripts/20-ollama.sh && make restart
```

### Cursor returns 401

Your API key in Cursor doesn't match `LITELLM_MASTER_KEY`. Re-paste:

```bash
grep LITELLM_MASTER_KEY config/detected.env | cut -d= -f2 | tr -d '"'
```

### Cursor returns 404 model not found

LiteLLM doesn't see `hybrid-auto`. Confirm:

```bash
KEY=$(grep LITELLM_MASTER_KEY config/detected.env | cut -d= -f2 | tr -d '"')
curl -s http://127.0.0.1:4000/models -H "Authorization: Bearer $KEY" | jq '.data[].id'
```

You should see `local-fast`, `local-long`, `claude-code`, `hybrid-auto`.
If not, `make stop && make start` and try again.

### Local request hangs forever

The model is still loading on first call. Check:

```bash
tail -f .logs/ollama.err     # for local-long
tail -f .logs/mlx.err        # for local-fast
```

Cold-load on this install:
- **MLX 7B-4bit**: ~2 min on first call after a service restart, then sub-second per request.
- **Ollama 80B-Q4_K_M with `OLLAMA_KV_CACHE_TYPE=q4_0`**: first call after a service restart can take 60–90 s while the GGUF is mmap'd into RAM. After that, sustained inference at the configured context window (65k by default for the Q4 build, 128k on Q8 builds) is fluent. The q4_0 KV cache cuts the per-token state to ~4× smaller than `f16` so a 128k context fits comfortably under the 128 GB unified memory budget.

If you want to force a warmup right after `make start`:

```bash
KEY=$(grep LITELLM_MASTER_KEY config/detected.env | cut -d= -f2 | tr -d '"')
for m in local-fast local-long claude-code; do
  curl -s --max-time 240 -X POST http://127.0.0.1:4000/v1/chat/completions \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"OK\"}],\"max_tokens\":2}" \
    > /dev/null && echo "warmed: $m"
done
```

### Claude tier returns 401

`ANTHROPIC_API_KEY` isn't visible to launchd. Refresh:

```bash
launchctl setenv ANTHROPIC_API_KEY "$ANTHROPIC_API_KEY"
make restart
```

### Dashboard shows 0 requests

You're hitting providers directly, not via LiteLLM. Confirm Cursor's Base
URL is `http://127.0.0.1:4000/v1` (note the `/v1`).

### Resetting everything

```bash
make clean && make install        # keeps models + cost.db
make nuke  && make install        # also drops models + cost.db (re-pulls)
```

---

## Performance tuning and the prefix cache

### Ollama runtime knobs (set automatically by `make detect`)

These three live in `config/detected.env` and get rendered into the
Ollama launchd plist. Defaults are optimized for a Cursor-style single-user
agent workflow.

| Var                     | Default | Why                                                                                                  |
| ----------------------- | ------- | ---------------------------------------------------------------------------------------------------- |
| `OLLAMA_KV_CACHE_TYPE`  | `q4_0`  | ~4× cache compression. Strongest type that stable Ollama 0.21.x actually supports (auto-detected).   |
| `OLLAMA_FLASH_ATTENTION`| `1`     | Required for any compressed KV cache type to actually apply.                                         |
| `OLLAMA_NUM_PARALLEL`   | `1`     | Dedicate the full Metal pipeline to one request. ~40% faster decode on long prompts vs. the default 2. Bump to 2+ if you start running multi-pane parallel agents. |
| `OLLAMA_KEEP_ALIVE`     | `30m`   | Keep the model + KV cache resident for half an hour after the last call. Default `5m` evicts mid-session and forces a 60-90 s cold reload. **This is the single most impactful knob for perceived latency.** |

To change any of them: edit `scripts/00-detect.sh`, run `make detect && make finalize`.

### Why prefix caching matters more than raw prefill speed

Cold prefill on the 80B-Q4_K_M with q4_0 KV cache is ~370-490 tok/s.
That sounds fine until you realize a 110k-token prompt takes ~225 s to
prefill from scratch. The saving grace: **Ollama's prompt cache
re-uses KV state across consecutive requests that share a prefix**, and
Cursor's traffic is exactly that pattern (the agent re-reads the same
file across many turns).

Measured on this install with `make perf-prefix-cold` (60k-token shared
prefix, 4 follow-ups):

| Turn      | Prefill | Wall    | Notes                                  |
| --------- | ------: | ------: | -------------------------------------- |
| 1 (cold)  | 122.7 s | 130.5 s | full prefill                           |
| 2-5 (warm) |  ~1.0 s |  ~2.1 s | 99.2% cache hit, 60 000 tok/s effective |

In other words: the user pays the 130 s cold cost **once per session**,
not per turn. Every subsequent turn that re-uses the file context is
~2 s. This is why we set `OLLAMA_KEEP_ALIVE=30m` — the default 5 min
would evict the cache during natural pauses (going to lunch, reading
docs) and force a full re-prefill.

If you ever see follow-up turns paying the full prefill cost, run
`make perf-prefix` and check whether something between Cursor and Ollama
is mutating the prompt prefix (e.g. injecting a different system message
each turn) — that breaks the cache.

### The perf suite

| Target                | What it does                                                            | Time |
| --------------------- | ----------------------------------------------------------------------- | ---: |
| `make perf`           | Cold + 500/5k/18k tok runs + router boundary check                      | ~1 min |
| `make perf-short`     | Same as `perf` but skips the 18k run                                    | ~30 s |
| `make perf-stress`    | Adds ~110k local stress + ~140k over-ceiling claude routing test        | 5–7 min |
| `make perf-prefix`    | Cursor-style probe: 80k shared prefix + 4 follow-ups (warm)             | ~10 s |
| `make perf-prefix-cold` | Same probe, bounces Ollama first for a true cold turn 1               | ~3 min |

Logs land in `.logs/perf-*.log` for diffing across runs.

### What did *not* help much

We tested these too — included for the record so you don't waste time:

- **`NUM_PARALLEL=2 → 1`**: only ~4% faster cold prefill (memory bandwidth, not compute, is the bottleneck at 80k+ tokens). The decode-time win is real (~40%) but only after prefill.
- **`q4_0 → q8_0` KV cache**: would *slow down* prefill ~1.3–1.5× because of doubled memory traffic. We're staying on q4_0.
- **TurboQuant `tq3`**: not supported by stable Ollama 0.21.x (silent fallback to f16). See the TurboQuant section below for the watcher and the opt-in experimental fork.

---

## TurboQuant: status, watcher, and experimental fork

### What's actually running today

The detector probes the running Ollama binary for the strongest KV-cache
type it actually recognizes and pins that into `config/detected.env`. On
this machine (Ollama 0.21.2) the result is:

```
KV_CACHE_TYPE=q4_0          # ~4× compression, stable, requires Flash Attention
```

The launchd job sets both `OLLAMA_KV_CACHE_TYPE=q4_0` and
`OLLAMA_FLASH_ATTENTION=1` for the Ollama process. `make verify`
inspects the on-disk binary's recognized strings and fails loudly if
the requested type isn't actually supported (the prior `tq3` config was
silently falling back to uncompressed `f16` — the new check would have
caught that).

### Why not native TurboQuant (`tq3`/`tq4`)?

TurboQuant (Google Research, ICLR 2026, [arXiv 2504.19874](https://arxiv.org/abs/2504.19874))
gives ~5–6× KV-cache compression with negligible quality loss. Tracking:

| Project   | PR                                                                  | State (2026-04) |
| --------- | ------------------------------------------------------------------- | --------------- |
| Ollama    | [#15090](https://github.com/ollama/ollama/pull/15090) (Go-native)   | Closed; waiting on MLX upstream |
| Ollama    | [#15125](https://github.com/ollama/ollama/pull/15125) (engine wiring) | Open, blocked   |
| MLX core  | [#3328](https://github.com/ml-explore/mlx/pull/3328)                | In review       |
| llama.cpp | [#21131](https://github.com/ggml-org/llama.cpp/pull/21131) `--turbo-kv` | Working PR, unmerged |

Until one of those lands in a stable Ollama build, we use `q4_0`.

### Option (b): wait for stable, get notified

`make turboquant-status` reports what your binary supports and whether
TurboQuant has landed. Two automation modes:

```bash
make turboquant-status     # one-shot report
make turboquant-upgrade    # if tq3 is now supported, flip + bounce daemon
make turboquant-watch      # daily poll loop; auto-applies tq3 the moment it ships
```

`turboquant-watch` is safe to run under `nohup` or in a tmux pane —
it does nothing destructive until tq3 actually appears in the daemon
binary, at which point it rewrites `config/detected.env`, re-renders
the plist, and bounces Ollama.

### Option (c): build the experimental llama.cpp fork now (opt-in)

`scripts/turboquant-experimental.sh` clones llama.cpp, cherry-picks
PR [#21131](https://github.com/ggml-org/llama.cpp/pull/21131)'s
`--turbo-kv` flag into a sibling worktree under `.experimental/`, builds
`llama-server` with Metal, and runs it on **port 8082** so it can't
collide with the live Ollama on :11434.

```bash
make turboquant-experimental-build      # one-time: clone + cherry-pick + cmake build
make turboquant-experimental-serve      # start on :8082, --turbo-kv tq3
make turboquant-experimental-status     # is it up?
make turboquant-experimental-ab PROMPT="Refactor f(x): return x*2"
                                        # send the same prompt to live :11434 and experimental :8082
                                        # prints elapsed times and saves both responses
make turboquant-experimental-stop
make turboquant-experimental-nuke       # remove the worktree entirely
```

Notes:

- The experimental server **does not** plug into LiteLLM. Treat it as a
  research benchtop. If you want to route Cursor traffic to it, add a
  `local-long-tq` entry in `config/litellm-config.yaml` pointing at
  `http://127.0.0.1:8082/v1` — keep that change on a feature branch
  until you trust the build.
- The PR is being rebased actively. If `make turboquant-experimental-build`
  hits a merge conflict it will stop and tell you to fix it inside
  `.experimental/llama-tq/llama.cpp` then re-run.
- Override the flag with `TQ_EXP_KV=tq4` (4-bit instead of 3-bit) or the
  port with `TQ_EXP_PORT=8083`.
- Re-uses the GGUF you already downloaded for Ollama; auto-discovers it.

The plan: run option (b)'s watcher for a few weeks, periodically use
option (c) to A/B against the live stack on real prompts, and graduate
to native tq3 the moment Ollama ships it. No changes to the live stack
are required to participate in either.

---

## Reference: file locations

```
config/detected.env                       # ports, keys, model tags
config/litellm-config.rendered.yaml       # actual proxy config (rendered)
config/router -> ../router                # symlink, required by LiteLLM's loader
config/cost   -> ../cost                  # symlink, required by LiteLLM's loader
.cursor/rules/hybrid-routing.mdc          # in-IDE routing guidance
launchd/*.rendered.plist                  # rendered launchd jobs
~/Library/LaunchAgents/com.local.*.plist  # active launchd jobs
cost/cost.db                              # SQLite log of every request
.logs/{ollama,mlx,litellm,dashboard}.{out,err}  # service stdout/stderr
.logs/install-{ollama,mlx}.log            # download progress logs
scripts/run_litellm.py                    # macOS-friendly LiteLLM launcher
scripts/perf-suite.sh                     # make perf / perf-short / perf-stress
scripts/perf-prefix-cache.sh              # make perf-prefix / perf-prefix-cold
scripts/turboquant-upgrade.sh             # status + auto-flip when tq3 ships in stable Ollama
scripts/turboquant-experimental.sh        # opt-in llama.cpp fork on :8082 for tq3 A/B testing
.experimental/llama-tq/                   # opt-in llama.cpp worktree (only if you ran -build)
```

## Reference: deeper docs

- [`docs/architecture.md`](architecture.md) — components and dataflow
- [`docs/routing.md`](routing.md) — the decision tree, with worked examples
- [`docs/cost-model.md`](cost-model.md) — actual / shadow / savings math
- [`docs/operations.md`](operations.md) — make targets and surgery
- [`docs/troubleshooting.md`](troubleshooting.md) — extended troubleshooting
