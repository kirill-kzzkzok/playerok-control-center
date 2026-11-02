CREATE TABLE IF NOT EXISTS chat_cards (
 account_id TEXT NOT NULL,
 owner_id INTEGER NOT NULL REFERENCES administrators(user_id),
 chat_id TEXT NOT NULL,
 token TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL DEFAULT 'NEW' CHECK(state IN ('NEW','SENDING','OPEN','UNCERTAIN','CLOSED')),
 telegram_message_id INTEGER,
 fingerprint TEXT,
 hint TEXT NOT NULL DEFAULT '',
 reply_operation TEXT REFERENCES outbox(id),
 busy_token TEXT,
 busy_until REAL NOT NULL DEFAULT 0,
 retry_after REAL NOT NULL DEFAULT 0,
 failures INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(account_id,owner_id,chat_id)
);
