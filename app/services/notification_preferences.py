from app.database.store import Conflict, encode, now

LABELS = {
    "messages": "Сообщения покупателей",
    "orders": "Изменения заказов",
    "problems": "Проблемы в сделках",
    "stock": "Остаток на складе",
    "promotions": "Остановка продвижения по лимиту",
    "enrollment": "Блокировка автоматической выдачи",
}
KINDS = {
    "message": "messages",
    "order": "orders",
    "order_delivered": "orders",
    "review": "orders",
    "order_problem": "problems",
    "stock_empty": "stock",
    "stock_low": "stock",
    "promotion_guard": "promotions",
    "enrollment_blocked": "enrollment",
    "delivery_delayed": "enrollment",
}


def notification_baseline(store):
    """Persist the startup boundary before backfill, never move it on recovery."""
    with store.transaction():
        value = store.setting("notifications:baseline")
        if value:
            return value
        if store.setting("notifications:initialized", False):
            return None  # Existing installations already established their baseline.
        if not store.rows("SELECT 1 FROM administrators LIMIT 1"):
            return None
        value = {"at": now(), "sequence": store.rows("SELECT COALESCE(MAX(rowid),0) AS n FROM inbox")[0]["n"]}
        store.set_setting("notifications:baseline", value)
        return value


class NotificationPreferences:
    def __init__(self, store):
        self.store = store

    def owner(self, db, actor):
        if not db.execute("SELECT 1 FROM administrators WHERE user_id=?", (actor,)).fetchone():
            raise Conflict("Доступ закрыт")

    def get(self, owner):
        value = self.store.setting(f"notifications:{owner}:preferences", {"revision": 0, "enabled": {}})
        if (
            not isinstance(value, dict)
            or type(value.get("revision")) is not int
            or value["revision"] < 0
            or not isinstance(value.get("enabled"), dict)
            or set(value["enabled"]) - LABELS.keys()
            or any(type(enabled) is not bool for enabled in value["enabled"].values())
        ):
            raise ValueError("Настройки уведомлений повреждены; требуется проверка локальной БД")
        return value["revision"], {key: value["enabled"].get(key, True) for key in LABELS}

    def enabled(self, owner, event_kind):
        if event_kind in {"operation_uncertain", "worker_failed"}:
            return True
        key = KINDS.get(event_kind)
        return self.get(owner)[1][key] if key else True

    def set(self, owner, revision, key, enabled):
        if key not in LABELS or type(enabled) is not bool:
            raise ValueError("Неизвестный тип уведомления или значение")
        with self.store.transaction() as db:
            self.owner(db, owner)
            current, values = self.get(owner)
            if current != revision:
                raise Conflict("Настройки изменились. Откройте уведомления заново")
            values[key] = enabled
            db.execute(
                "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"notifications:{owner}:preferences", encode({"revision": current + 1, "enabled": values})),
            )
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'NOTIFICATION_PREFERENCE',?,?,?)",
                (owner, key, "ENABLED" if enabled else "DISABLED", now()),
            )
            db.execute("DELETE FROM settings WHERE key=?", (f"notifications:{owner}:preferences_error",))
            if not enabled:
                kinds = [kind for kind, preference in KINDS.items() if preference == key]
                db.execute(
                    "UPDATE notifications SET status='SUPPRESSED',send_retry_at=0 WHERE owner_id=? "
                    "AND status='PENDING' AND event_key IN (SELECT key FROM inbox WHERE kind IN ("
                    + ",".join("?" for _ in kinds)
                    + "))",
                    (owner, *kinds),
                )
