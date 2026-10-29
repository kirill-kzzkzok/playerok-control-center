CREATE TABLE IF NOT EXISTS chat_uploads(
    operation_id TEXT NOT NULL REFERENCES outbox(id),
    position INTEGER NOT NULL,
    client_attachment_id TEXT NOT NULL UNIQUE,
    attachment_id TEXT,
    expires_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('PROCESSING','UPLOADED')),
    PRIMARY KEY(operation_id,position)
);
