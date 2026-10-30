CREATE TABLE IF NOT EXISTS auto_reply_rules(
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES administrators(user_id),
    account_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    trigger_text TEXT NOT NULL,
    response_text TEXT NOT NULL,
    cooldown INTEGER NOT NULL,
    daily_limit INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auto_reply_events(
    account_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    rule_id TEXT,
    rule_revision INTEGER,
    status TEXT NOT NULL,
    operation_id TEXT REFERENCES outbox(id),
    error_code TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id,message_id)
);
CREATE INDEX IF NOT EXISTS auto_reply_limits ON auto_reply_events(account_id,chat_id,status,created_at);
