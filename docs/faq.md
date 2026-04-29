# FAQ

### Why both MLX and Ollama? Isn't that redundant?

They optimize for opposite ends of the prompt-size spectrum. MLX wins on
short prompts (lower latency, faster decode). Ollama wins on long prompts
because it ships TurboQuant KV-cache compression (`tq3`), which MLX
currently lacks. The router picks the right one per request.

### What is TurboQuant?

A KV-cache quantization scheme from Google Research that compresses the
attention key/value tensors to ~3 bits per value while preserving most of
the model's quality. For an 80B model at 128k tokens of context, this is
the difference between "fits in 96 GB unified memory" and "doesn't run on
your laptop". See `OLLAMA_KV_CACHE_TYPE=tq3` in the install scripts.

### Does any of my code leave the machine?

Only when the router picks the `claude` tier. Local-fast and local-long
keep the prompt and the response on the device. The dashboard, the cost DB
and the A/B comparator all live on `127.0.0.1`.

### Can I run this on Intel Macs / Linux / Windows?

The project targets Apple Silicon. MLX is Apple-only; Ollama runs anywhere
but loses most of its appeal without unified memory. The Python pieces
(router, cost DB, dashboard) are portable, and you could repurpose them
in front of a different local backend.

### How accurate is the savings number?

Within a few percent of reality, assuming Anthropic's sticker prices match
your contract. The math is simple — token counts × per-token rates — and
the test suite asserts the exact numbers (`tests/test_cost.py`). The two
soft spots are:

1. We use `chars / 3.6` to estimate tokens *before* a request. The
   downstream `record_request` uses the **real** token counts from the
   provider's `usage` field, so the recorded `shadow_cost` is exact.
2. If Anthropic changes pricing, edit the constants in `cost/ingest.py`
   and `config/litellm-config.yaml`.

### Why SQLite and not Postgres?

Single-user, single-machine, append-mostly. SQLite is faster for this
shape and has zero ops cost.

### Can I share `cost.db` across machines?

Technically yes (rsync or a syncthing share works), but you'll get write
conflicts if two machines write at once. If you want a fleet view, each
machine should write locally and a separate process should aggregate.

### How do I force Cursor to use Claude only?

Two options:
- Switch the model in Cursor to `claude-code`.
- Prefix prompts with `[claude]`.

### How do I force Cursor to never use Claude?

Set the default model to `local-fast` (or `local-long`). The router won't
upgrade you without a complexity match — and even those hits don't reach
Claude unless you explicitly route to `hybrid-auto` or `claude-code`.

### What's the lowest RAM I can run this on?

`scripts/00-detect.sh` falls back to `qwen3-coder:30b` for hosts with less
than 48 GB. It works but the long-context experience suffers — the 30B
model only reaches ~32k tokens before quality drops noticeably. 64 GB+ is
the sweet spot; 96 GB+ unlocks 8-bit MoE quantization.

### Why is the A/B "judge score" so simple?

It's a sniff test: did the local model produce something similar in length
and structure to Claude? It catches gross failures (empty output, runaway
generation, missing code fences) but won't catch subtle correctness
issues. Use it as a heuristic, not a benchmark.

If you need real eval, point a benchmarking suite at the proxy — every
request is OpenAI-compatible.

### Can I plug in my own model?

Yes. Add it under `model_list` in `config/litellm-config.yaml` and
optionally extend `router/route_by_size.py` to route to it. Many people
add `gpt-4o` or `deepseek-v3` as a third cloud option.

### Why does `make stop` sometimes leave a stale process?

`launchctl unload` doesn't always SIGKILL. If `make status` shows the port
is still alive after `make stop`, find and kill manually:
```bash
lsof -i :4000
kill -9 <pid>
```

### Does the dashboard need to be running for cost tracking?

No. The router callback writes to SQLite directly. The dashboard is purely
a viewer; you can stop it (`launchctl unload ~/Library/LaunchAgents/com.local.dashboard.plist`)
and the rest of the stack keeps tracking.

### How do I reset everything?

```bash
make nuke && make install
```

`nuke` removes Ollama models, MLX cache and `cost.db`. The next `install`
re-pulls models and writes a fresh DB.
