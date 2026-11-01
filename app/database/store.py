import itertools
import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path


def now():
    return datetime.now(UTC).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def server_time(value):
    """Canonical aware time for SQL sorting; a receive timestamp is not a server date."""
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC).isoformat(timespec="microseconds")
    except (ValueError, TypeError, OverflowError):
        return None


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._savepoint = iter(itertools.count())
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
        self.db.create_function("server_time", 1, server_time, deterministic=True)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
        self.db.execute("INSERT OR IGNORE INTO schema_migrations VALUES(1,?)", (now(),))
        self.apply_migrations()

    def apply_migrations(self):
        directory = Path(__file__).with_name("migrations")
        for source in sorted(directory.glob("[0-9]*.sql")):
            version = int(source.name.split("_", 1)[0])
            with self.transaction() as db:
                if db.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
                    continue
                statement = ""
                for line in source.read_text(encoding="utf-8").splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        db.execute(statement)
                        statement = ""
                if statement.strip():
                    raise RuntimeError("Незавершённая SQL-миграция")
                db.execute("INSERT INTO schema_migrations VALUES(?,?)", (version, now()))

    @contextmanager
    def transaction(self):
        """Outer call: BEGIN IMMEDIATE .. COMMIT/ROLLBACK. Nested call (a transaction
        opened while this connection already holds one): SAVEPOINT/RELEASE/ROLLBACK,
        so service-level transactions compose into one atomic unit without changes."""
        with self.lock:
            if self.db.in_transaction:
                name = f"sp_{next(self._savepoint)}"
                self.db.execute(f"SAVEPOINT {name}")
                try:
                    yield self.db
                    self.db.execute(f"RELEASE SAVEPOINT {name}")
                except BaseException:
                    self.db.execute(f"ROLLBACK TO SAVEPOINT {name}")
                    self.db.execute(f"RELEASE SAVEPOINT {name}")
                    raise
                return
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

    def enqueue(self, key, kind, target, payload, db=None, actor=None):
        if db is None:
            with self.transaction() as connection:
                return self.enqueue(key, kind, target, payload, db=connection, actor=actor)
        connection = db
        ident = uuid.uuid4().hex
        with self.lock:
            existing = connection.execute("SELECT id FROM outbox WHERE operation_key=?", (key,)).fetchone()
            if existing:
                return existing[0]
            if kind in {"delivery", "delivery_text", "chain_message", "fulfill"}:
                gift = connection.execute(
                    "SELECT state FROM gift_jobs WHERE order_id=?", (target,)
                ).fetchone()
                if gift and (kind != "fulfill" or gift[0] != "SUCCEEDED"):
                    raise Conflict(
                        "Для заказа уже зарегистрирована подарочная выдача; другая выдача запрещена"
                    )
            if kind in {"delivery", "delivery_text", "fulfill"}:
                chain = connection.execute(
                    "SELECT order_id FROM delivery_chains WHERE order_id=?", (target,)
                ).fetchone()
                if chain and payload.get("chain_id") != target and kind != "fulfill":
                    raise Conflict("Для заказа уже зарегистрирована цепочка выдачи")
                if (
                    chain
                    and kind == "fulfill"
                    and connection.execute(
                        "SELECT 1 FROM delivery_chains WHERE order_id=? AND status!='NEEDS_FULFILLMENT' AND status!='SUCCEEDED'",
                        (target,),
                    ).fetchone()
                ):
                    raise Conflict("Цепочка выдачи ещё не подтверждена")
            if kind in {"delivery", "fulfill"}:
                if (
                    kind == "fulfill"
                    and connection.execute(
                        "SELECT 1 FROM outbox WHERE target=? AND kind='fulfill' AND error_code='MANUALLY_STOPPED_UNCERTAIN' LIMIT 1",
                        (target,),
                    ).fetchone()
                ):
                    raise Conflict("Исход предыдущей отметки неизвестен; новая запись запрещена")
                if connection.execute(
                    "SELECT 1 FROM outbox WHERE target=? AND kind IN ('delivery','fulfill') AND status IN ('PENDING','PROCESSING','RETRYABLE_FAILED','UNCERTAIN') LIMIT 1",
                    (target,),
                ).fetchone():
                    raise Conflict("Для заказа есть незавершённая выдача или отметка выполнения")
            if kind in {
                "create",
                "publish",
                "edit",
                "edit_fields",
                "discount",
                "delete",
                "agreements",
                "premium_publish",
                "premium_bump",
            }:
                blocked = connection.execute(
                    "SELECT id FROM outbox WHERE target=? AND kind IN ('create','publish','edit','edit_fields','discount','delete','agreements','premium_publish','premium_bump') "
                    "AND status IN ('PENDING','PROCESSING','RETRYABLE_FAILED','UNCERTAIN') LIMIT 1",
                    (target,),
                ).fetchone()
                if blocked:
                    raise Conflict(
                        "Для этого товара уже есть незавершённая операция. Сначала проверьте её результат."
                    )
            connection.execute(
                "INSERT OR IGNORE INTO outbox(id,operation_key,kind,target,payload,created_at) VALUES(?,?,?,?,?,?)",
                (ident, key, kind, target, encode(payload), now()),
            )
            if kind in {"delete", "edit_fields"} or (kind == "edit" and "description" in payload):
                connection.execute(
                    "UPDATE gift_listings SET enabled=0,revision=revision+1 WHERE item_id=? AND enabled=1",
                    (target,),
                )
            if actor is None and payload.get("chain_id"):
                chain = connection.execute(
                    "SELECT actor FROM delivery_chains WHERE order_id=?", (target,)
                ).fetchone()
                actor = chain[0] if chain else None
            if actor is not None and kind in {
                "create",
                "publish",
                "edit",
                "edit_fields",
                "discount",
                "delete",
                "delivery",
                "delivery_text",
                "fulfill",
                "gift_send",
                "gift_refund",
                "gift_notice",
                "premium_publish",
                "premium_bump",
            }:
                connection.execute(
                    "INSERT OR IGNORE INTO operation_receipts(operation_id,owner_id) SELECT id,? FROM outbox WHERE operation_key=? AND EXISTS(SELECT 1 FROM administrators WHERE user_id=?)",
                    (actor, key, actor),
                )
            return connection.execute("SELECT id FROM outbox WHERE operation_key=?", (key,)).fetchone()[0]

    def claim(self, lease_seconds=60, *, kinds=None, operation_id=None):
        with self.transaction() as db:
            filters, args = "", []
            if kinds is not None:
                if not kinds:
                    return None
                filters += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
                args.extend(kinds)
            if operation_id is not None:
                filters += " AND id=?"
                args.append(operation_id)
            row = db.execute(
                "SELECT * FROM outbox WHERE status IN ('PENDING','RETRYABLE_FAILED')"
                + filters
                + " ORDER BY created_at LIMIT 1",
                args,
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE outbox SET status='PROCESSING',lease_until=?,lease_token=?,attempts=attempts+1 WHERE id=?",
                (time.time() + lease_seconds, token, row["id"]),
            )
            return {**dict(row), "lease_token": token, "payload": json.loads(row["payload"])}

    @staticmethod
    def release_launch(db, ident, target, payload_json, status, error=None):
        """Release a terminally failed launch: draft back to DRAFT (+revision), drop the marker.

        Shared by automatic finish() and manual owner resolution; UNCERTAIN never releases.
        """
        try:
            payload = json.loads(payload_json) if isinstance(payload_json, str) else dict(payload_json or {})
        except ValueError:
            return
        launch_id = payload.get("launch_id")
        # Only a proven-local or server-rejected failure releases the launch: local
        # guards never sent anything, and an HTTP 4xx means the server refused the
        # request (no publication happened). Anything else — network errors, unknown
        # results, manual stops — keeps the launch blocked for reconciliation.
        releasable = error in {None, "VALIDATION_FAILED"} or (
            isinstance(error, str) and error.startswith("HTTP_4")
        )
        if status != "PERMANENT_FAILED" or not launch_id or not releasable:
            return
        marker_row = db.execute(
            "SELECT value FROM settings WHERE key=?", ("launch:prepared:" + target,)
        ).fetchone()
        marker = json.loads(marker_row[0]) if marker_row else {}
        matching_marker = marker.get("operation") == ident and marker.get("id") == launch_id
        # A persisted older PREMIUM payload may carry the revision only in its
        # marker. Read it before deleting anything, and never borrow another
        # operation's authority to disarm delivery.
        delivery_revision = payload.get("delivery_revision")
        if delivery_revision is None and matching_marker:
            delivery_revision = marker.get("delivery_revision")
        launch_revision = payload.get("launch_revision")
        if launch_revision is None and matching_marker:
            launch_revision = marker.get("revision")
        released = db.execute(
            "UPDATE drafts SET state='DRAFT',revision=revision+1 "
            "WHERE id=? AND state='QUEUED' AND (? IS NULL OR revision=?)",
            (launch_id, launch_revision, launch_revision),
        )
        if released.rowcount != 1:
            return
        authority_key = "promotions:launch_authority:" + target
        authority_row = db.execute("SELECT value FROM settings WHERE key=?", (authority_key,)).fetchone()
        authority = json.loads(authority_row[0]) if authority_row else {}
        if authority.get("launch_id") == launch_id and authority.get("launch_revision") == launch_revision:
            db.execute("DELETE FROM settings WHERE key=?", (authority_key,))
        # CAS on the launch's own delivery revision: only rows still at the revision
        # the launch committed are disarmed. A newer owner edit (independently
        # re-armed delivery) survives untouched - no lost owner update.
        if delivery_revision is not None:
            db.execute(
                "UPDATE gift_listings SET enabled=0,revision=revision+1 "
                "WHERE item_id=? AND enabled=1 AND revision=?",
                (target, delivery_revision),
            )
            policy_changed = db.execute(
                "UPDATE delivery_policies SET payload=json_set(payload,'$.enabled',0),"
                "revision=revision+1,updated_at=? "
                "WHERE item_id=? AND json_extract(payload,'$.enabled')=1 AND revision=?",
                (now(), target, delivery_revision),
            )
            if policy_changed.rowcount == 1:
                db.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (f"delivery:{target}:enabled", "false"),
                )
        # Without a recorded delivery revision there is no authority to disarm an
        # independently configured listing. Legacy release must not guess.
        if matching_marker:
            db.execute("DELETE FROM settings WHERE key=?", ("launch:prepared:" + target,))

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
            row = db.execute(
                "SELECT operation_key,kind,target,payload FROM outbox WHERE id=?", (ident,)
            ).fetchone()
            self.release_launch(db, ident, row["target"], row["payload"], status, error=error)
            if row["kind"] in {"premium_publish", "premium_bump"}:
                from app.services.paid_promotions import finish_expense

                finish_expense(
                    db,
                    ident,
                    "SUCCEEDED"
                    if status == "SUCCEEDED"
                    else "UNCERTAIN"
                    if status == "UNCERTAIN"
                    else "FAILED",
                )
            if row["kind"] in {"gift_send", "gift_refund"}:
                from app.gifts.outcomes import finish

                finish(db, {**dict(row), "id": ident}, status, result, error, store=self)
            if row[0].startswith("delivery:"):
                order = row[0].removeprefix("delivery:")
                state = "DELIVERED" if status == "SUCCEEDED" else "QUARANTINED"
                db.execute(
                    "UPDATE inventory SET status=? WHERE id=(SELECT inventory_id FROM reservations WHERE order_id=?)",
                    (state, order),
                )
                current_order = db.execute("SELECT status FROM orders WHERE id=?", (order,)).fetchone()
                if current_order:
                    db.execute(
                        "INSERT INTO order_history(order_id,source,new_status,note,created_at) VALUES(?,'DELIVERY_RESULT',?,?,?)",
                        (order, current_order[0], status, now()),
                    )
            if status == "UNCERTAIN":
                self.uncertain_event(db, ident)
            if row["kind"] == "fulfill":
                db.execute(
                    "INSERT INTO order_history(order_id,source,new_status,note,created_at) SELECT id,'FULFILLMENT_RESULT',status,?,? FROM orders WHERE id=?",
                    (status, now(), row["target"]),
                )

    @staticmethod
    def uncertain_event(db, ident):
        row = db.execute("SELECT kind,target,error_code FROM outbox WHERE id=?", (ident,)).fetchone()
        payload = {"operation_id": ident, **dict(row)}
        db.execute(
            "INSERT OR IGNORE INTO inbox(key,kind,payload,received_at,processed_at) VALUES(?,'operation_uncertain',?,?,?)",
            (f"operation:{ident}:UNCERTAIN", encode(payload), now(), now()),
        )

    def recover(self):
        with self.transaction() as db:
            affected = db.execute(
                "SELECT id,operation_key,kind,target FROM outbox WHERE status='PROCESSING' AND lease_until<?",
                (time.time(),),
            ).fetchall()
            db.execute(
                "UPDATE outbox SET status='UNCERTAIN',error_code='LEASE_EXPIRED' WHERE status='PROCESSING' AND lease_until<?",
                (time.time(),),
            )
            for row in affected:
                if row["kind"] in {"premium_publish", "premium_bump"}:
                    from app.services.paid_promotions import finish_expense

                    finish_expense(db, row["id"], "UNCERTAIN")
                if row["kind"] in {"gift_send", "gift_refund"}:
                    from app.gifts.outcomes import recover

                    recover(db, row)
                if row["operation_key"].startswith("delivery:"):
                    db.execute(
                        "UPDATE inventory SET status='QUARANTINED' WHERE id=(SELECT inventory_id FROM reservations WHERE order_id=?)",
                        (row["operation_key"].removeprefix("delivery:"),),
                    )
                    order = row["operation_key"].removeprefix("delivery:")
                    db.execute(
                        "INSERT INTO order_history(order_id,source,new_status,note,created_at) SELECT id,'DELIVERY_RESULT',status,'UNCERTAIN: LEASE_EXPIRED',? FROM orders WHERE id=?",
                        (now(), order),
                    )
                self.uncertain_event(db, row["id"])
                if row["kind"] == "fulfill":
                    db.execute(
                        "INSERT INTO order_history(order_id,source,new_status,note,created_at) SELECT id,'FULFILLMENT_RESULT',status,'UNCERTAIN: LEASE_EXPIRED',? FROM orders WHERE id=?",
                        (now(), row["target"]),
                    )
            return len(affected)

    def audit(self, actor, action, target, outcome):
        self.execute(
            "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,?,?,?,?)",
            (actor, action, target, outcome, now()),
        )

    def record_late_result(self, ident, token, status, result=None, error=None):
        """Retain evidence after recovery/resolution, without replacing authoritative state."""
        if status not in {"SUCCEEDED", "RETRYABLE_FAILED", "UNCERTAIN", "PERMANENT_FAILED"}:
            raise ValueError("Недопустимое наблюдение операции")
        allowed = {"message_id", "remote_id", "chat_id", "status", "image_count", "confirmed", "removed"}
        safe = {
            key: value
            for key, value in (result.items() if isinstance(result, dict) else ())
            if key in allowed and isinstance(value, (str, int, bool))
        }
        with self.transaction() as db:
            row = db.execute("SELECT status,lease_token FROM outbox WHERE id=?", (ident,)).fetchone()
            if not row or (row["status"] == "PROCESSING" and row["lease_token"] == token):
                raise Conflict("Не подтверждена потеря lease")
            inserted = db.execute(
                "INSERT OR IGNORE INTO outbox_late_results VALUES(?,?,?,?,?,?)",
                (ident, token, status, encode(safe), error, now()),
            ).rowcount
            if inserted:
                db.execute(
                    "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(NULL,'LATE_OUTBOX_RESULT',?,?,?)",
                    (ident, status, now()),
                )

    def backup(self, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, closing(sqlite3.connect(destination)) as target:
            self.db.backup(target)

    def close(self):
        with self.lock:
            self.db.close()
