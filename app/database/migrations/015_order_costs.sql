CREATE TABLE IF NOT EXISTS order_costs(
    order_id TEXT PRIMARY KEY REFERENCES orders(id),
    account_id TEXT NOT NULL,
    cost TEXT,
    other_expenses TEXT,
    revision INTEGER NOT NULL,
    review_fingerprint TEXT,
    actor INTEGER NOT NULL REFERENCES administrators(user_id),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_expense_allocations(
    account_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    order_id TEXT NOT NULL REFERENCES orders(id),
    amount TEXT NOT NULL,
    actor INTEGER NOT NULL REFERENCES administrators(user_id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id,transaction_id,order_id),
    FOREIGN KEY(account_id,transaction_id) REFERENCES account_transactions(account_id,id)
);
CREATE INDEX IF NOT EXISTS allocations_order ON order_expense_allocations(order_id);
