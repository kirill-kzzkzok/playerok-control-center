CREATE TABLE IF NOT EXISTS auto_enrollment(
    order_id TEXT NOT NULL REFERENCES orders(id),
    policy_revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING','CREATED','SKIPPED','BLOCKED')),
    due_at REAL NOT NULL DEFAULT 0,
    lease_until REAL,
    lease_token TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(order_id,policy_revision)
);
