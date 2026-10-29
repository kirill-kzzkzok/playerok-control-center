import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def now():
    return datetime.now(UTC).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
        self.db.execute("INSERT OR IGNORE INTO schema_migrations VALUES(1,?)", (now(),))

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    def setting(self, key, default=None):
        rows = self.rows("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_setting(self, key, value):
        self.execute(
            "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, encode(value)),
        )

    def claim_first_owner(self, user_id):
        if user_id <= 0:
            return False
        with self.transaction() as db:
            current = db.execute("SELECT user_id FROM administrators LIMIT 1").fetchone()
            if current:
                return current[0] == user_id
            db.execute("INSERT INTO administrators VALUES(?)", (user_id,))
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,?,?,?,?)",
                (user_id, "FIRST_START_OWNER_CLAIM", str(user_id), "SUCCEEDED", now()),
            )
            return True

    def draft(self, owner, payload):
        ident = uuid.uuid4().hex
        self.execute(
            "INSERT INTO drafts(id,owner_id,payload,created_at) VALUES(?,?,?,?)",
            (ident, owner, encode(payload), now()),
        )
        return ident

    def get_draft(self, ident, owner):
        rows = self.rows("SELECT * FROM drafts WHERE id=? AND owner_id=?", (ident, owner))
        if not rows:
            raise Conflict("Черновик недоступен")
        row = rows[0]
        row["payload"] = json.loads(row["payload"])
        return row

    def update_draft(self, ident, owner, revision, payload):
        result = self.execute(
            "UPDATE drafts SET payload=?,revision=revision+1 WHERE id=? AND owner_id=? AND revision=? AND state='DRAFT'",
            (encode(payload), ident, owner, revision),
        )
        if result.rowcount != 1:
            raise Conflict("Черновик изменён в другом сценарии; откройте его заново")

    def enqueue(self, key, kind, target, payload, db=None):
        connection = db or self.db
        ident = uuid.uuid4().hex
        with self.lock:
            connection.execute(
                "INSERT OR IGNORE INTO outbox(id,operation_key,kind,target,payload,created_at) VALUES(?,?,?,?,?,?)",
                (ident, key, kind, target, encode(payload), now()),
            )
            return connection.execute("SELECT id FROM outbox WHERE operation_key=?", (key,)).fetchone()[0]

    def claim(self, lease_seconds=60):
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM outbox WHERE status IN ('PENDING','RETRYABLE_FAILED') ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE outbox SET status='PROCESSING',lease_until=?,lease_token=?,attempts=attempts+1 WHERE id=?",
                (time.time() + lease_seconds, token, row["id"]),
            )
            return {**dict(row), "lease_token": token, "payload": json.loads(row["payload"])}

    def finish(self, ident, token, status, result=None, error=None):
        if status not in {"SUCCEEDED", "RETRYABLE_FAILED", "UNCERTAIN", "PERMANENT_FAILED"}:
            raise ValueError("Недопустимый результат операции")
        with self.transaction() as db:
            r = db.execute(
                "UPDATE outbox SET status=?,result=?,error_code=?,lease_until=NULL WHERE id=? AND lease_token=? AND status='PROCESSING'",
                (status, encode(result), error, ident, token),
            )
            if r.rowcount != 1:
                raise Conflict("Lease операции потерян")
            # Delivery state and external outcome must commit together.
            row = db.execute("SELECT operation_key FROM outbox WHERE id=?", (ident,)).fetchone()
            if row[0].startswith("delivery:"):
                order = row[0].removeprefix("delivery:")
                state = "DELIVERED" if status == "SUCCEEDED" else "QUARANTINED"
                db.execute(
                    "UPDATE inventory SET status=? WHERE id=(SELECT inventory_id FROM reservations WHERE order_id=?)",
                    (state, order),
                )

    def recover(self):
        with self.transaction() as db:
            affected = db.execute(
                "SELECT operation_key FROM outbox WHERE status='PROCESSING' AND lease_until<?", (time.time(),)
            ).fetchall()
            db.execute(
                "UPDATE outbox SET status='UNCERTAIN',error_code='LEASE_EXPIRED' WHERE status='PROCESSING' AND lease_until<?",
                (time.time(),),
            )
            for row in affected:
                if row[0].startswith("delivery:"):
                    db.execute(
                        "UPDATE inventory SET status='QUARANTINED' WHERE id=(SELECT inventory_id FROM reservations WHERE order_id=?)",
                        (row[0].removeprefix("delivery:"),),
                    )
            return len(affected)

    def audit(self, actor, action, target, outcome):
        self.execute(
            "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,?,?,?,?)",
            (actor, action, target, outcome, now()),
        )

    def backup(self, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, sqlite3.connect(destination) as target:
            self.db.backup(target)

    def close(self):
        with self.lock:
            self.db.close()
