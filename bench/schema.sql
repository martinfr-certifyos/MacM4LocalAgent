-- Idempotent. Tables that hold benchmark runs across all three arms
-- (local-only, claude-only, cursor-without-proxy).

CREATE TABLE IF NOT EXISTS bench_runs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              INTEGER NOT NULL,                -- unix seconds
  task_id         TEXT    NOT NULL,                -- bench/tasks/<id>.json
  arm             TEXT    NOT NULL,                -- 'local-only' | 'claude-only' | 'cursor-no-proxy' | 'cursor-hybrid'
  model           TEXT    NOT NULL,                -- raw model id reported by backend
  attempt         INTEGER NOT NULL DEFAULT 1,
  -- Tokens + cost (real $ for arms that hit Anthropic, 0 for local).
  input_tok       INTEGER NOT NULL DEFAULT 0,
  output_tok      INTEGER NOT NULL DEFAULT 0,
  actual_cost     REAL    NOT NULL DEFAULT 0.0,
  shadow_cost     REAL    NOT NULL DEFAULT 0.0,    -- what Claude would have charged
  -- Timing.
  wall_ms         INTEGER NOT NULL DEFAULT 0,      -- end-to-end (incl. eval/grading)
  generate_ms     INTEGER NOT NULL DEFAULT 0,      -- model generate only
  ttft_ms         INTEGER NOT NULL DEFAULT 0,      -- time to first token (0 if unknown)
  grade_ms        INTEGER NOT NULL DEFAULT 0,      -- pytest run time
  -- Output + grading.
  output_chars    INTEGER NOT NULL DEFAULT 0,
  output_path     TEXT    NOT NULL DEFAULT '',     -- saved generated module
  pytest_passed   INTEGER NOT NULL DEFAULT 0,
  pytest_failed   INTEGER NOT NULL DEFAULT 0,
  pytest_errors   INTEGER NOT NULL DEFAULT 0,
  pytest_total    INTEGER NOT NULL DEFAULT 0,
  passes_tests    REAL    NOT NULL DEFAULT 0.0,    -- pass_count / total
  no_thirdparty   INTEGER NOT NULL DEFAULT 0,      -- 0 or 1
  has_docstring   INTEGER NOT NULL DEFAULT 0,
  has_type_hints  INTEGER NOT NULL DEFAULT 0,
  syntactic_ok    INTEGER NOT NULL DEFAULT 0,
  composite_score REAL    NOT NULL DEFAULT 0.0,    -- weighted [0,1]
  -- Free-form context.
  notes           TEXT    NOT NULL DEFAULT '',
  raw_metadata    TEXT    NOT NULL DEFAULT '{}'    -- json blob
);

CREATE INDEX IF NOT EXISTS idx_bench_runs_ts      ON bench_runs(ts);
CREATE INDEX IF NOT EXISTS idx_bench_runs_arm     ON bench_runs(arm);
CREATE INDEX IF NOT EXISTS idx_bench_runs_task    ON bench_runs(task_id);

-- One row per (task, arm) summarizing N attempts. Refreshed by reporter.
CREATE TABLE IF NOT EXISTS bench_summary (
  task_id         TEXT NOT NULL,
  arm             TEXT NOT NULL,
  attempts        INTEGER NOT NULL DEFAULT 0,
  pass_rate       REAL    NOT NULL DEFAULT 0.0,
  mean_score      REAL    NOT NULL DEFAULT 0.0,
  median_wall_ms  INTEGER NOT NULL DEFAULT 0,
  median_gen_ms   INTEGER NOT NULL DEFAULT 0,
  total_input_tok INTEGER NOT NULL DEFAULT 0,
  total_output_tok INTEGER NOT NULL DEFAULT 0,
  total_actual    REAL    NOT NULL DEFAULT 0.0,
  total_shadow    REAL    NOT NULL DEFAULT 0.0,
  -- Provider-billed reconciliation (filled by collectors).
  provider_billed_usd REAL NOT NULL DEFAULT 0.0,   -- what Anthropic / Cursor actually charged
  provider_source     TEXT NOT NULL DEFAULT '',    -- 'anthropic-admin-api' | 'cursor-admin-api' | 'manual-paste' | ''
  PRIMARY KEY (task_id, arm)
);

-- Raw provider spend snapshots, keyed to a benchmark window. These are the
-- ground truth for "what did the provider actually charge us during the run?".
CREATE TABLE IF NOT EXISTS provider_spend (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            INTEGER NOT NULL,                    -- when we fetched the snapshot
  arm           TEXT    NOT NULL,                    -- which run arm this anchors to
  task_id       TEXT    NOT NULL DEFAULT '',         -- '' = whole window
  window_start  INTEGER NOT NULL,                    -- unix sec, run start
  window_end    INTEGER NOT NULL,                    -- unix sec, run end
  provider      TEXT    NOT NULL,                    -- 'anthropic' | 'cursor'
  source        TEXT    NOT NULL,                    -- 'admin-api' | 'usage-events' | 'manual'
  -- Token / request totals attributed to this window.
  input_tok     INTEGER NOT NULL DEFAULT 0,
  output_tok    INTEGER NOT NULL DEFAULT 0,
  cache_read_tok  INTEGER NOT NULL DEFAULT 0,
  cache_write_tok INTEGER NOT NULL DEFAULT 0,
  requests      INTEGER NOT NULL DEFAULT 0,
  -- Money (USD, post any plan discount).
  billed_usd    REAL    NOT NULL DEFAULT 0.0,
  -- Identity scoping.
  api_key_id    TEXT    NOT NULL DEFAULT '',         -- Anthropic api_key_id if scoped
  user_email    TEXT    NOT NULL DEFAULT '',         -- Cursor member email if scoped
  raw_response  TEXT    NOT NULL DEFAULT '{}'        -- json blob, untruncated
);

CREATE INDEX IF NOT EXISTS idx_provider_spend_window ON provider_spend(window_start, window_end);
CREATE INDEX IF NOT EXISTS idx_provider_spend_arm    ON provider_spend(arm);
