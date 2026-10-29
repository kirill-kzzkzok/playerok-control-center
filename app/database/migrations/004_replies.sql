CREATE TABLE IF NOT EXISTS quick_replies(
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quick_replies_owner ON quick_replies(owner_id,created_at,id);
CREATE TABLE IF NOT EXISTS notification_actions(
    operation_id TEXT PRIMARY KEY REFERENCES outbox(id),
    event_key TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    rendered_status TEXT,
    retry_after REAL NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(event_key,owner_id) REFERENCES notifications(event_key,owner_id)
);
