#!/usr/bin/env bash
# 50-cursor.sh - Drop a Cursor rule and print the base-URL/API-key/model
# instructions; open Cursor's settings since keys are encrypted on disk
# and cannot be set programmatically.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO_ROOT/config/detected.env"

log() { printf "\033[1;34m[cursor]\033[0m %s\n" "$*"; }

RULE_DIR="$REPO_ROOT/.cursor/rules"
mkdir -p "$RULE_DIR"
cat > "$RULE_DIR/hybrid-routing.mdc" <<EOF
---
description: When to use local-fast vs local-long vs claude-code
alwaysApply: true
---

# Hybrid local + cloud routing

This workspace runs a hybrid local+cloud LLM setup behind LiteLLM
(\`http://127.0.0.1:${LITELLM_PORT}\`). Pick the right tier for the prompt.

## Models exposed

- **local-fast** — MLX, Qwen3-Coder-Next, ≤16k context, ~70 tok/s, free.
- **local-long** — Ollama + TurboQuant tq3 KV cache, ≤${LOCAL_LONG_CTX} ctx, free.
- **claude-code** — Anthropic Claude Sonnet 4.6, 1M context, \$3 in / \$15 out per 1M.
- **hybrid-auto** — let the router decide based on size + heuristic complexity.

## Decision tree

\`\`\`
Is the prompt explicitly tagged?
├─ "[claude] ..." ────────────────► claude-code   (force cloud)
└─ "[local]  ..." ────────────────► local-fast / local-long  (force local)

Else, by content:
├─ Architectural / multi-file / deep reasoning ──► claude-code
├─ ≤16k tokens                                   ──► local-fast
├─ 16k–128k tokens                               ──► local-long
└─ >128k tokens                                  ──► claude-code
\`\`\`

## Worked examples

| Prompt                                              | Tier         | Why                                |
| --------------------------------------------------- | ------------ | ---------------------------------- |
| "Rename \`foo\` to \`bar\` in this file."           | local-fast   | tiny, single-file                  |
| "Summarize this 60k-token codebase dump."           | local-long   | size only, not architectural       |
| "Refactor the auth subsystem across 12 services."   | claude-code  | architectural + multi-file         |
| "[local] Refactor across multiple files."           | local-fast   | tag overrides classifier           |
| "[claude] hello world"                              | claude-code  | tag overrides size                 |

## Cost model

Every request is logged to \`cost/cost.db\` with both \`actual_cost\` (real
USD; 0 for local) and \`shadow_cost\` (what Claude would have charged).
Dashboard: <http://127.0.0.1:${DASHBOARD_PORT}>. CLI: \`make report\`.

## Defaults

- Set **hybrid-auto** as your default model in Cursor; the router picks the
  cheapest tier that can carry the prompt.
- Switch to **claude-code** explicitly when you want the strongest output
  (architecture reviews, gnarly bugs, designs).
- Use **local-long** for any "read-this-large-thing" task — staying local
  is free and fast on Apple Silicon with TurboQuant.

## Don't

- Don't paste secrets into prompts. \`local-*\` keeps them on-device, but
  \`claude-code\` will send them to Anthropic.
- Don't disable the proxy and call providers directly; you'll lose cost
  tracking and the savings dashboard will show 0.
EOF
log "wrote $RULE_DIR/hybrid-routing.mdc"

cat <<EOF

==================================================================
  Cursor wiring (manual one-time setup; keys are stored encrypted)
==================================================================

  1. Open Cursor -> Settings (Cmd+,) -> Models
  2. Toggle "Override OpenAI Base URL" ON
  3. Base URL:  http://127.0.0.1:${LITELLM_PORT}/v1
  4. API Key:   ${LITELLM_MASTER_KEY}
  5. Click "+ Add Model" four times and add:
       - local-fast
       - local-long
       - claude-code
       - hybrid-auto    (auto-routes based on prompt size + complexity)
  6. Click Verify; pick your default model.

  NOTE: Cursor's Agent mode currently ignores custom keys
        (Ask and Plan modes work today). The dashboard at
        http://127.0.0.1:${DASHBOARD_PORT} will only show traffic
        that goes through your custom models.

  Trying to open Settings now...
EOF

if [[ -d "/Applications/Cursor.app" ]]; then
  open -a "Cursor" || true
else
  log "Cursor.app not found in /Applications; open it manually."
fi
