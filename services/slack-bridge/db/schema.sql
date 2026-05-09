-- agent-me slack-bridge state schema
--
-- Stored at: ${AGENT_ME_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-me}/state.db
--
-- Tables:
--   threads             one row per Slack thread the bridge has touched
--   messages            full transcript per thread (user/assistant/tool turns)
--   pending_approvals   in-flight review-by-default approval prompts
--
-- All timestamps are unix seconds (INTEGER) for easy comparison and zero
-- timezone ambiguity.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS threads (
  thread_ts       TEXT PRIMARY KEY,
  channel         TEXT NOT NULL,
  user_id         TEXT,
  auto_approve    INTEGER NOT NULL DEFAULT 0,
  created_at      INTEGER NOT NULL,
  last_active_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_ts   TEXT NOT NULL,
  role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
  content     TEXT,
  slack_ts    TEXT,
  created_at  INTEGER NOT NULL,
  FOREIGN KEY (thread_ts) REFERENCES threads(thread_ts)
);

CREATE INDEX IF NOT EXISTS idx_messages_thread_ts ON messages(thread_ts);

CREATE TABLE IF NOT EXISTS pending_approvals (
  id                TEXT PRIMARY KEY,
  thread_ts         TEXT NOT NULL,
  action_type       TEXT,
  payload_json      TEXT,
  status            TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
  slack_message_ts  TEXT,
  created_at        INTEGER NOT NULL,
  resolved_at       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_pending_approvals_status ON pending_approvals(status);
