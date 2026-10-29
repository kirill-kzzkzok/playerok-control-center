CREATE TABLE IF NOT EXISTS sync_requests(
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('chat','deal','catchup')),
    target TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    completed_revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    not_before REAL NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    error_code TEXT
);
