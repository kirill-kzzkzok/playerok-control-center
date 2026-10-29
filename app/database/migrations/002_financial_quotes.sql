CREATE TABLE IF NOT EXISTS financial_quotes(id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, target TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS financial_quotes_target ON financial_quotes(owner_id,target,created_at);
