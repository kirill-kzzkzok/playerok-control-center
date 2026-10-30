CREATE TABLE gift_listings (
 item_id TEXT PRIMARY KEY,
 owner_id INTEGER NOT NULL REFERENCES administrators(user_id),
 gift_code TEXT NOT NULL,
 gift_id TEXT NOT NULL,
 approved_stars INTEGER NOT NULL CHECK(approved_stars>0),
 description_sha256 TEXT NOT NULL,
 category_id TEXT NOT NULL,
 obtaining_type_id TEXT NOT NULL,
 username_field_id TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 auto_fulfill INTEGER NOT NULL DEFAULT 0 CHECK(auto_fulfill IN (0,1)),
 revision INTEGER NOT NULL DEFAULT 1,
 armed_at TEXT
);
CREATE TABLE gift_jobs (
 order_id TEXT PRIMARY KEY REFERENCES orders(id),
 item_id TEXT NOT NULL REFERENCES gift_listings(item_id),
 operation_id TEXT NOT NULL UNIQUE REFERENCES outbox(id),
 listing_revision INTEGER NOT NULL,
 gift_code TEXT NOT NULL,
 gift_id TEXT NOT NULL,
 reference TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL DEFAULT 'PENDING' CHECK(state IN
 ('PENDING','CHECKING','SPENDING','SUCCEEDED','BLOCKED','INVALID_RECIPIENT','UNCERTAIN')),
 recipient_username TEXT,
 recipient_id INTEGER,
 payer_id INTEGER,
 recipient_access_hash TEXT,
 stars INTEGER,
 form_id TEXT UNIQUE,
 spent_at TEXT,
 message_id INTEGER,
 receipt TEXT,
 error_code TEXT,
 refund_state TEXT NOT NULL DEFAULT 'NONE' CHECK(refund_state IN
 ('NONE','MANUAL','QUEUED','SPENDING','CONFIRMED','UNCERTAIN','FAILED')),
 refund_operation_id TEXT UNIQUE REFERENCES outbox(id),
 completion_operation_id TEXT UNIQUE REFERENCES outbox(id),
 completion_state TEXT NOT NULL DEFAULT 'WAITING' CHECK(completion_state IN ('WAITING','MANUAL','QUEUED')),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX gift_jobs_state ON gift_jobs(state,created_at);
CREATE TABLE gift_catalog (
 code TEXT PRIMARY KEY,
 emoji TEXT NOT NULL,
 gift_id TEXT NOT NULL UNIQUE,
 stars INTEGER NOT NULL CHECK(stars>0),
 available INTEGER NOT NULL CHECK(available IN (0,1)),
 checked_at TEXT NOT NULL
);
