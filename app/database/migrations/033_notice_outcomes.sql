ALTER TABLE notifications ADD COLUMN send_retry_at REAL NOT NULL DEFAULT 0;
ALTER TABLE notifications ADD COLUMN send_error TEXT;
CREATE TABLE operation_receipts_next(
    operation_id TEXT NOT NULL REFERENCES outbox(id),
    owner_id INTEGER NOT NULL REFERENCES administrators(user_id),
    notice_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(notice_status IN ('PENDING','PROCESSING','SENT','UNCERTAIN','REJECTED')),
    telegram_message_id INTEGER,
    view_message_id INTEGER,
    view_token TEXT,
    rendered_status TEXT,
    send_retry_at REAL NOT NULL DEFAULT 0,
    send_error TEXT,
    PRIMARY KEY(operation_id,owner_id)
);
INSERT INTO operation_receipts_next(operation_id,owner_id,notice_status,telegram_message_id,view_message_id,view_token,rendered_status)
SELECT operation_id,owner_id,notice_status,telegram_message_id,view_message_id,view_token,rendered_status FROM operation_receipts;
DROP TABLE operation_receipts;
ALTER TABLE operation_receipts_next RENAME TO operation_receipts;
CREATE INDEX receipts_owner_message ON operation_receipts(owner_id,telegram_message_id);
