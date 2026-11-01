CREATE TABLE logical_products (
 id TEXT PRIMARY KEY,
 code TEXT NOT NULL UNIQUE CHECK(length(code)=8 AND substr(code,1,1)='#' AND substr(code,2) NOT GLOB '*[^0-9]*'),
 owner_id INTEGER NOT NULL REFERENCES administrators(user_id),
 account_id TEXT NOT NULL,
 handler TEXT NOT NULL CHECK(handler IN ('telegram_gifts','unique_stock','fixed_text','manual')),
 config TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1,
 control_revision INTEGER NOT NULL DEFAULT 0,
 enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 inherit_enabled INTEGER NOT NULL DEFAULT 0 CHECK(inherit_enabled IN (0,1)),
 state TEXT NOT NULL CHECK(state IN ('LEGACY','PENDING','VERIFIED','BLOCKED','DETACHED')),
 armed_at TEXT,
 stock_item_id TEXT NOT NULL,
 baseline TEXT NOT NULL,
 original_description TEXT NOT NULL,
 error_code TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE logical_listings (
 item_id TEXT PRIMARY KEY,
 product_id TEXT NOT NULL REFERENCES logical_products(id),
 config_revision INTEGER NOT NULL,
 description_sha256 TEXT NOT NULL,
 server_status TEXT NOT NULL,
 verified_at TEXT,
 operation_id TEXT UNIQUE REFERENCES outbox(id)
);
CREATE INDEX logical_listings_product ON logical_listings(product_id);
CREATE TABLE logical_order_scopes (
 order_id TEXT PRIMARY KEY REFERENCES orders(id),
 product_id TEXT NOT NULL REFERENCES logical_products(id),
 config_revision INTEGER NOT NULL,
 item_id TEXT NOT NULL,
 stock_item_id TEXT NOT NULL,
 config TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE logical_scan_items (
 account_id TEXT NOT NULL,
 item_id TEXT NOT NULL,
 generation TEXT NOT NULL,
 payload TEXT,
 checked_at TEXT,
 PRIMARY KEY(account_id,item_id)
);
