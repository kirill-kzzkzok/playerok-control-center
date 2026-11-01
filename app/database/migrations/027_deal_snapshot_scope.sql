ALTER TABLE logical_listings ADD COLUMN source_order_id TEXT REFERENCES orders(id);
CREATE UNIQUE INDEX logical_listing_order_snapshot ON logical_listings(source_order_id) WHERE source_order_id IS NOT NULL;
