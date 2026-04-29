# Cursor IDE integration

Cursor is OpenAI-compatible. Point it at the LiteLLM proxy and pick
`hybrid-auto` as the default model. Done.

## One-time setup

1. Open **Cursor → Settings → Models**.
2. Add a custom OpenAI-compatible provider:
   - **Base URL:** `http://127.0.0.1:4000`
   - **API key:** the value of `LITELLM_MASTER_KEY` (echo it with
     `grep MASTER_KEY config/detected.env`).
3. Add these model aliases (any that you intend to use directly):
   - `hybrid-auto` ← **set this as the default**
   - `local-fast`
   - `local-long`
   - `claude-code`
4. Save and restart Cursor.

`scripts/50-cursor.sh` will print this checklist and try to open the Models
pane for you.

## Forcing a specific tier

Two ways:

- **Switch model in Cursor** for the rest of the session.
- **Inline tag in your prompt:**
  - `[local]` → forces a local tier (fast or long, by size).
  - `[claude]` → forces `claude-code` regardless of size.

The tag wins over the size + complexity heuristics. See
[routing.md](routing.md) for the full decision tree.

## Cursor Agent Mode caveat

Cursor's **Agent mode** does not currently honour custom OpenAI-compatible
providers for *all* features (tool use, file edits, etc.). The chat box and
inline edits work fine; agent mode falls back to Cursor's hosted models.

If you need agent-mode against your local stack, two options:

1. Use Cursor's chat + composer panel, which routes through the proxy.
2. Use the LiteLLM proxy directly via `curl` or a thin CLI:
   ```bash
   curl -s http://127.0.0.1:4000/v1/chat/completions \
     -H "Authorization: Bearer $(grep MASTER_KEY config/detected.env | cut -d= -f2)" \
     -H "Content-Type: application/json" \
     -d '{"model":"hybrid-auto","messages":[{"role":"user","content":"hi"}]}'
   ```

We re-evaluate the agent-mode story whenever Cursor ships new model APIs.

## Cursor rules file

`.cursor/rules/hybrid-routing.mdc` lives next to your project and describes
the tier strategy in machine-readable form. Cursor surfaces it as guidance
to the in-IDE agent. The file is created by `scripts/50-cursor.sh`; edit it
freely, it's a normal markdown file.

## Verifying that Cursor is actually using LiteLLM

Send a one-line prompt from Cursor, then run:

```bash
sqlite3 cost/cost.db "SELECT model, tier, route_reason, input_tok, output_tok \
  FROM requests ORDER BY id DESC LIMIT 1;"
```

You should see the `tier` LiteLLM picked. If the table is empty, your
request never reached the proxy — re-check the Base URL and master key.

## Per-project overrides

Cursor's `.cursor/settings.json` supports a `models.endpoint` override that
takes precedence over global settings. Useful when a single repo wants a
different LiteLLM instance (say, a remote Mac mini on your network):

```jsonc
{
  "models": {
    "endpoint": "http://mini.local:4000",
    "apiKey":   "..."
  }
}
```
