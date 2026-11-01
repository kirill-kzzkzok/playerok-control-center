ALTER TABLE operation_receipts ADD COLUMN view_message_id INTEGER;
ALTER TABLE operation_receipts ADD COLUMN view_token TEXT;
ALTER TABLE operation_receipts ADD COLUMN rendered_status TEXT;
ALTER TABLE notifications ADD COLUMN rendered_payload TEXT;
ALTER TABLE notifications ADD COLUMN refresh_after REAL NOT NULL DEFAULT 0;
ALTER TABLE notifications ADD COLUMN refresh_failures INTEGER NOT NULL DEFAULT 0;
CREATE TABLE chat_message_versions(id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, payload TEXT NOT NULL, observed_at TEXT NOT NULL);
