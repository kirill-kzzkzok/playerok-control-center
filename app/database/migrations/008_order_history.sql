CREATE TABLE IF NOT EXISTS order_annotations(
    order_id TEXT PRIMARY KEY REFERENCES orders(id),
    problem INTEGER NOT NULL DEFAULT 0 CHECK(problem IN (0,1)),
    note TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    actor INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_history(
    id INTEGER PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id),
    source TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL,
    problem INTEGER,
    actor INTEGER,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS order_history_order ON order_history(order_id,id);
INSERT INTO order_history(order_id,source,new_status,note,created_at)
SELECT id,'IMPORTED',status,'Состояние при миграции; предыдущая история не восстанавливается',strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM orders;
