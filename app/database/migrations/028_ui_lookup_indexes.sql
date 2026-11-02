CREATE INDEX IF NOT EXISTS notifications_owner_message ON notifications(owner_id,telegram_message_id);
CREATE INDEX IF NOT EXISTS receipts_owner_message ON operation_receipts(owner_id,telegram_message_id);
