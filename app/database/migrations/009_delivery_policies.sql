CREATE TABLE IF NOT EXISTS delivery_policies(
    item_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    actor INTEGER NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_runs(
    id INTEGER PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id),
    policy_revision INTEGER NOT NULL,
    actor INTEGER NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
);
