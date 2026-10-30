CREATE TABLE IF NOT EXISTS account_transactions(
    account_id TEXT NOT NULL,
    id TEXT NOT NULL,
    operation TEXT NOT NULL,
    direction TEXT NOT NULL,
    status TEXT NOT NULL,
    value TEXT,
    currency TEXT NOT NULL,
    created_at TEXT,
    completed_at TEXT,
    deal_id TEXT,
    reported_fee TEXT,
    fee_percent TEXT,
    withdrawal_net_rub TEXT,
    observed_at TEXT NOT NULL,
    PRIMARY KEY(account_id,id)
);
CREATE INDEX IF NOT EXISTS transactions_completed ON account_transactions(account_id,completed_at);
CREATE INDEX IF NOT EXISTS transactions_deal ON account_transactions(account_id,deal_id);
