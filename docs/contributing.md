# Contributing

Short and pragmatic. The repo is small enough that you can hold most of it
in your head; this page just sets the conventions so additions stay
consistent.

## Ground rules

- **Idempotent installs.** Every `scripts/*.sh` must be safe to re-run. Use
  guards like `command -v ollama || brew install ollama`.
- **No state outside `cost/cost.db` and `models/`.** If a feature needs new
  state, add it to the SQLite schema; don't write to `~/Library`.
- **Tests stay green.** `make test` must pass before you push. PRs that
  add new behavior should add tests.
- **Don't break the public CLI.** `make help`, `make report --json`, and the
  `/api/stats` endpoint are stable surfaces.

## Dev loop

```bash
git clone <repo> && cd MacM4LocalAgent
make detect          # generate detected.env on your hardware
make install         # one-time
make test            # 100+ tests; should be ~2 seconds
```

When you change Python code:
```bash
make test-py
```

When you change shell:
```bash
make test-sh
```

When you change the dashboard templates, just refresh the browser; the
template cache is disabled in dev.

## Code style

- **Python**: ruff (`make lint`), `from __future__ import annotations`,
  type hints on all public functions.
- **Shell**: `set -euo pipefail` at the top of every script. Use `log()` and
  `warn()` helpers (defined in `scripts/_lib.sh` if you add one). Always
  quote variables.
- **YAML / config**: 2-space indent, no trailing spaces.
- **Markdown**: 100-char soft wrap, fenced code blocks with language
  identifiers.

`make lint` runs ruff over the Python tree and `bash -n` over every script.

## Adding a new tier

Suppose you want a `local-vision` tier for an MLX vision-language model.

1. **Register the model** in `config/litellm-config.yaml` under
   `model_list`. Mirror the `local-fast` entry.
2. **Update `decide_tier`** in `router/route_by_size.py` so an
   image-bearing request maps to `local-vision`.
3. **Update `_record`** in the same file so its tier classification handles
   the new model name.
4. **Update `cost/savings.py`** if the new tier needs special grouping
   (most don't — `tier` is just a string).
5. **Add a launchd plist** under `launchd/` if it's a new server process.
   Include `@@REPO_ROOT@@` placeholders.
6. **Tests:** new cases in `tests/test_router.py` for the routing decision,
   plus a happy-path test in `tests/test_compare.py` if it's part of the A/B.

## Adding a new dashboard page

1. New route in `dashboard/app.py` — copy the pattern of an existing one.
2. New template in `dashboard/templates/` — extend `_layout.html`.
3. Test in `tests/test_dashboard.py` using `TestClient`.

## PR checklist

- [ ] `make test` green locally
- [ ] `make lint` clean (or comments why a finding is a false positive)
- [ ] New behavior is documented in `docs/` and linked from
      `docs/README.md`
- [ ] CHANGELOG.md has an entry under "Unreleased"
- [ ] If you changed `cost/schema.sql`, the migration is backwards-compatible
      (additive only — no `DROP COLUMN`)

## Commit message format

`<area>: <imperative summary>` — small, focused commits. Example:

```
router: add [tools] tag override for forced tool use
cost: add ts index to requests for faster window queries
docs: clarify Cursor agent-mode caveat
```

If a change spans many files, prefer one commit per *concept*, not one
giant catch-all.

## Releasing

`make test` green → tag → push.
`CHANGELOG.md` under "Unreleased" gets renamed to the new version with the
date. We don't have automated release plumbing yet; do it by hand.
