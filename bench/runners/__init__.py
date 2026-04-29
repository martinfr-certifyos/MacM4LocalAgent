"""Per-arm benchmark runners.

local_only.py     - hits LiteLLM model `local-long` (Ollama / Qwen3-Coder)
claude_only.py    - hits LiteLLM model `claude-code` (Anthropic via proxy)
cursor_session.py - ingests a recorded Cursor session (no proxy, just the
                    Cursor backend) - human-in-the-loop, but the cost / timing
                    fields are filled from the user's session log + the
                    provider-spend collector.
"""
