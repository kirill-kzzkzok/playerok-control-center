CREATE TABLE draft_stock_imports(
    draft_id TEXT PRIMARY KEY REFERENCES drafts(id),
    item_id TEXT NOT NULL,
    owner_id INTEGER NOT NULL REFERENCES administrators(user_id),
    imported_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
