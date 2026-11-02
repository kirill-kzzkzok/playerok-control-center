CREATE TABLE IF NOT EXISTS logical_code_reservations(
 code TEXT PRIMARY KEY COLLATE NOCASE CHECK(length(code)=3 AND code=upper(code) AND code NOT GLOB '*[^A-Z0-9]*'),
 product_id TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS logical_code_no_delete BEFORE DELETE ON logical_code_reservations BEGIN
 SELECT RAISE(ABORT,'Permanent title code cannot be reused');
END;
CREATE TRIGGER IF NOT EXISTS logical_code_immutable BEFORE UPDATE OF code,product_id ON logical_code_reservations BEGIN
 SELECT RAISE(ABORT,'Permanent title code is immutable');
END;
