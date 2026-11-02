CREATE TABLE logical_products_next (
 id TEXT PRIMARY KEY,
 code TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK((length(code)=3 AND code NOT GLOB '*[^A-Z0-9]*') OR (length(code)=8 AND substr(code,1,1)='#' AND substr(code,2) NOT GLOB '*[^0-9]*')),
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
INSERT INTO logical_products_next SELECT * FROM logical_products;
DROP TABLE logical_products;
ALTER TABLE logical_products_next RENAME TO logical_products;
CREATE TABLE IF NOT EXISTS logical_code_migrations(
 product_id TEXT PRIMARY KEY REFERENCES logical_products(id),
 account_id TEXT NOT NULL,
 owner_id INTEGER NOT NULL REFERENCES administrators(user_id),
 old_code TEXT,
 new_code TEXT,
 state TEXT NOT NULL DEFAULT 'REQUIRED' CHECK(state IN ('REQUIRED','PLANNED','PENDING','PROCESSING','VERIFIED','BLOCKED','UNCERTAIN','MANUAL_REVIEW_REQUIRED')),
 plan TEXT,
 configuration_hash TEXT,
 was_enabled INTEGER NOT NULL CHECK(was_enabled IN (0,1)),
 revision INTEGER NOT NULL,
 control_revision INTEGER NOT NULL,
 confirmed_at TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO logical_code_migrations(product_id,account_id,owner_id,old_code,was_enabled,revision,control_revision,created_at,updated_at)
 SELECT id,account_id,owner_id,code,enabled,revision,control_revision,created_at,updated_at FROM logical_products WHERE length(code)=8;
CREATE TABLE IF NOT EXISTS logical_code_migration_items(
 item_id TEXT PRIMARY KEY,
 product_id TEXT NOT NULL REFERENCES logical_products(id),
 state TEXT NOT NULL DEFAULT 'PLANNED',
 before_snapshot TEXT NOT NULL,
 after_snapshot TEXT NOT NULL,
 operation_id TEXT REFERENCES outbox(id),
 error_code TEXT,
 verified_at TEXT,
 updated_at TEXT NOT NULL
);
