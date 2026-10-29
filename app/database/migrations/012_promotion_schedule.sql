CREATE TABLE IF NOT EXISTS promotion_schedule(
    item_id TEXT PRIMARY KEY,
    due_at REAL NOT NULL DEFAULT 0,
    lease_until REAL,
    lease_token TEXT,
    failures INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    checked_at TEXT,
    policy_revision INTEGER NOT NULL DEFAULT 0
);
