CREATE TABLE operation_receipts(
    operation_id TEXT NOT NULL REFERENCES outbox(id),
    owner_id INTEGER NOT NULL REFERENCES administrators(user_id),
    notice_status TEXT NOT NULL DEFAULT 'PENDING' CHECK(notice_status IN ('PENDING','PROCESSING','SENT','UNCERTAIN')),
    telegram_message_id INTEGER,
    PRIMARY KEY(operation_id,owner_id)
);
