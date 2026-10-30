from app.database.store import Conflict, encode, now

LABELS = {
    "messages": "Сообщения покупателей",
    "orders": "Изменения заказов",
    "problems": "Проблемы в сделках",
    "stock": "Пустой запас",
    "promotions": "Остановка продвижения по лимиту",
    "enrollment": "Блокировка автоматической выдачи",
}
KINDS = {
    "message": "messages",
    "order": "orders",
    "order_problem": "problems",
    "stock_empty": "stock",
    "promotion_guard": "promotions",
    "enrollment_blocked": "enrollment",
}


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
        if event_kind == "operation_uncertain":
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
