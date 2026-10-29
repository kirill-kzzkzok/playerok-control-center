CREATE TABLE IF NOT EXISTS delivery_chains(
    order_id TEXT PRIMARY KEY REFERENCES orders(id),
    item_id TEXT NOT NULL,
    actor INTEGER NOT NULL,
    policy_revision INTEGER NOT NULL,
    policy TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ACTIVE','SUCCEEDED','BLOCKED','CANCELLED','NEEDS_FULFILLMENT')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_chain_steps(
    order_id TEXT NOT NULL REFERENCES delivery_chains(order_id),
    position INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    operation_id TEXT UNIQUE REFERENCES outbox(id),
    PRIMARY KEY(order_id,position)
);
