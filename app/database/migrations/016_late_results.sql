CREATE TABLE IF NOT EXISTS outbox_late_results(
    operation_id TEXT NOT NULL REFERENCES outbox(id),
    lease_token TEXT NOT NULL,
    observed_status TEXT NOT NULL,
    result TEXT,
    error_code TEXT,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(operation_id,lease_token)
);
