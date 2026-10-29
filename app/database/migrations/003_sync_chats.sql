CREATE TABLE IF NOT EXISTS sync_chats(
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    last_synced_at TEXT,
    initial_complete INTEGER NOT NULL DEFAULT 0,
    error_code TEXT
);
CREATE INDEX IF NOT EXISTS sync_chats_schedule ON sync_chats(last_synced_at,id);
CREATE INDEX IF NOT EXISTS inbox_processed ON inbox(processed_at,received_at);
