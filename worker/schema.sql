CREATE TABLE trips (
  id TEXT PRIMARY KEY,
  line_group_id TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL, -- 'active' | 'ended'
  owner_line_user_id TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  ended_at INTEGER
);
CREATE INDEX idx_trips_group_status ON trips (line_group_id, status);
-- Enforces "at most one active trip per group" at the DB layer, closing the
-- check-then-insert race between two near-simultaneous /旅程 開始 commands.
CREATE UNIQUE INDEX idx_trips_one_active_per_group ON trips (line_group_id) WHERE status = 'active';

CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  trip_id TEXT NOT NULL REFERENCES trips(id),
  line_message_id TEXT NOT NULL UNIQUE,
  line_user_id TEXT NOT NULL,
  user_display_name TEXT,
  msg_type TEXT NOT NULL, -- text | image | video | sticker | ...
  text TEXT,
  sent_at INTEGER NOT NULL,
  organized_at INTEGER, -- NULL = not yet folded into trip_docs
  unsent_at INTEGER -- NULL = not recalled; set when LINE reports the sender unsent it
);
CREATE INDEX idx_messages_trip_organized ON messages (trip_id, organized_at);

CREATE TABLE llm_usage (
  id TEXT PRIMARY KEY,
  trip_id TEXT REFERENCES trips(id),
  purpose TEXT NOT NULL, -- 'organize' | 'answer'
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  estimated_cost_usd REAL NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE trip_docs (
  trip_id TEXT PRIMARY KEY REFERENCES trips(id),
  content_md TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE trip_doc_revisions (
  id TEXT PRIMARY KEY,
  trip_id TEXT NOT NULL REFERENCES trips(id),
  content_md TEXT NOT NULL,
  triggered_by_message_id TEXT REFERENCES messages(id),
  created_at INTEGER NOT NULL
);

CREATE TABLE attachments (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL REFERENCES messages(id),
  r2_key TEXT NOT NULL,
  content_type TEXT NOT NULL
);

CREATE TABLE expenses (
  id TEXT PRIMARY KEY,
  trip_id TEXT NOT NULL REFERENCES trips(id),
  payer_line_user_id TEXT NOT NULL,
  payer_display_name TEXT,
  amount REAL NOT NULL,
  description TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX idx_expenses_trip ON expenses (trip_id);

CREATE TABLE expense_splits (
  expense_id TEXT NOT NULL REFERENCES expenses(id),
  line_user_id TEXT NOT NULL,
  display_name TEXT,
  PRIMARY KEY (expense_id, line_user_id)
);

CREATE TABLE polls (
  id TEXT PRIMARY KEY,
  trip_id TEXT NOT NULL REFERENCES trips(id),
  topic TEXT NOT NULL,
  status TEXT NOT NULL, -- 'active' | 'ended'
  created_by_line_user_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  ended_at INTEGER
);
CREATE INDEX idx_polls_trip ON polls (trip_id, created_at);
-- Enforces "at most one active poll per trip" at the DB layer, same pattern as
-- idx_trips_one_active_per_group above.
CREATE UNIQUE INDEX idx_polls_one_active_per_trip ON polls (trip_id) WHERE status = 'active';

CREATE TABLE poll_options (
  id TEXT PRIMARY KEY,
  poll_id TEXT NOT NULL REFERENCES polls(id),
  text TEXT NOT NULL,
  created_at INTEGER NOT NULL -- option number is derived by ordering on this, not stored
);
CREATE INDEX idx_poll_options_poll ON poll_options (poll_id, created_at);

CREATE TABLE poll_votes (
  poll_option_id TEXT NOT NULL REFERENCES poll_options(id),
  line_user_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  voted_at INTEGER NOT NULL,
  PRIMARY KEY (poll_option_id, line_user_id)
);

-- LLM-Wiki style fact-check: after the hourly cron sweep organizes a batch, a cheap
-- second pass checks the new/changed doc content against the raw source messages and
-- flags anything unsupported/contradictory here for a human to review via /檢查.
CREATE TABLE doc_fact_check_flags (
  id TEXT PRIMARY KEY,
  trip_id TEXT NOT NULL REFERENCES trips(id),
  claim TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_fact_check_flags_trip ON doc_fact_check_flags (trip_id, created_at);

-- Runtime-adjustable settings (currently just which model each LLM stage uses), overriding
-- claude_client.py's DEFAULT_*_MODEL constants. Changing a value here takes effect on the
-- very next call - no redeploy needed. Absent key = use the default.
CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
