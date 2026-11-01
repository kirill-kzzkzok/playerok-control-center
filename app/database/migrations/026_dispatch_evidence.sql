ALTER TABLE outbox ADD COLUMN started_at TEXT;
ALTER TABLE outbox ADD COLUMN retry_at REAL NOT NULL DEFAULT 0;
UPDATE outbox SET started_at=created_at WHERE attempts>0 OR status<>'PENDING';
UPDATE outbox SET status='UNCERTAIN',error_code='LEGACY_DISPATCH_UNKNOWN' WHERE started_at IS NOT NULL AND status IN ('PENDING','RETRYABLE_FAILED');
INSERT OR IGNORE INTO inbox(key,kind,payload,received_at,processed_at) SELECT 'operation:'||id||':UNCERTAIN','operation_uncertain',json_object('operation_id',id,'kind',kind,'target',target,'code','LEGACY_DISPATCH_UNKNOWN'),created_at,created_at FROM outbox WHERE error_code='LEGACY_DISPATCH_UNKNOWN';
UPDATE inventory SET status='QUARANTINED' WHERE status='RESERVED' AND id IN (SELECT r.inventory_id FROM reservations r JOIN outbox o ON o.operation_key='delivery:'||r.order_id WHERE o.error_code='LEGACY_DISPATCH_UNKNOWN' AND o.status='UNCERTAIN');
