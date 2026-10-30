CREATE TABLE IF NOT EXISTS panel_messages(
    bot_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL REFERENCES administrators(user_id),
    message_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    touched_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','DELETING','DELETED','FAILED','UNCERTAIN')),
    cleanup_id TEXT,
    error_code TEXT,
    PRIMARY KEY(bot_id,owner_id,message_id)
);
CREATE INDEX IF NOT EXISTS panel_messages_cleanup ON panel_messages(bot_id,owner_id,status,touched_at);
