CREATE TABLE IF NOT EXISTS promotion_policy(
    id INTEGER PRIMARY KEY CHECK(id=1),
    revision INTEGER NOT NULL,
    actor INTEGER NOT NULL,
    payload TEXT NOT NULL,
    selection TEXT NOT NULL CHECK(selection IN ('selected','all')),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promotion_targets(
    item_id TEXT PRIMARY KEY,
    disposition TEXT NOT NULL CHECK(disposition IN ('include','exclude')),
    actor INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promotion_observations(
    id INTEGER PRIMARY KEY,
    item_id TEXT NOT NULL,
    policy_revision INTEGER NOT NULL,
    position INTEGER,
    checked_at TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS promotion_observed ON promotion_observations(item_id,policy_revision,id);
CREATE TABLE IF NOT EXISTS promotion_evaluations(
    id INTEGER PRIMARY KEY,
    item_id TEXT NOT NULL,
    policy_revision INTEGER NOT NULL,
    result TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
