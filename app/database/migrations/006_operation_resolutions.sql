CREATE TABLE IF NOT EXISTS operation_resolutions(
    operation_id TEXT PRIMARY KEY REFERENCES outbox(id),
    actor INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('CONFIRM_MESSAGE','CONFIRM_READ_STATE','STOP_UNCERTAIN')),
    reason TEXT NOT NULL,
    evidence TEXT NOT NULL,
    previous_error_code TEXT,
    previous_result TEXT,
    resolved_at TEXT NOT NULL
);
INSERT OR IGNORE INTO inbox(key,kind,payload,received_at,processed_at)
SELECT 'operation:'||id||':UNCERTAIN','operation_uncertain',
json_object('operation_id',id,'kind',kind,'target',target,'error_code',error_code),
strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM outbox WHERE status='UNCERTAIN';
