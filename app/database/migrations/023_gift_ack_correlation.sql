-- Old sends keep their visible-reference receipt contract. New sends switch to
-- MESSAGE_ID atomically with their pre-RPC intent; no historical send is retried.
ALTER TABLE gift_steps ADD COLUMN receipt_mode TEXT NOT NULL DEFAULT 'REFERENCE'
 CHECK(receipt_mode IN ('REFERENCE','MESSAGE_ID'));
ALTER TABLE gift_steps ADD COLUMN ack_message_id INTEGER CHECK(ack_message_id > 0);
CREATE UNIQUE INDEX gift_step_ack_identity
 ON gift_steps(payer_id,recipient_id,ack_message_id) WHERE ack_message_id IS NOT NULL;
