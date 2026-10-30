import hashlib
import json
import secrets
import time
from datetime import UTC, datetime

from app.database.store import Conflict, encode, now
from app.services.automation import Automation
from app.services.finance import checked_time
from app.services.products import ValidationError
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard

LISTS = {
    "enrollment": (
        "Будущие заказы · регистрация",
        "SELECT order_id AS ident,status||' · v'||policy_revision||' · '||COALESCE(error_code,'') AS label FROM auto_enrollment ORDER BY updated_at DESC,order_id",
        "order:",
    ),
    "policies": (
        "Политики выдачи",
        "SELECT item_id AS ident,CAST(revision AS TEXT) AS label FROM delivery_policies ORDER BY updated_at DESC,item_id",
        "auto:item:",
    ),
    "chains": (
        "Цепочки выдачи",
        "SELECT order_id AS ident,status AS label FROM delivery_chains ORDER BY created_at DESC,order_id",
        "aq:chain:",
    ),
    "targets": (
        "Выбор продвижения",
        "SELECT item_id AS ident,disposition AS label FROM promotion_targets ORDER BY updated_at DESC,item_id",
        "aq:target:",
    ),
    "schedule": (
        "Расписание dry-run",
        "SELECT item_id AS ident,COALESCE(error_code,'проверка запланирована') AS label FROM promotion_schedule ORDER BY due_at,item_id",
        "aq:target:",
    ),
    "expenses": (
        "Расходы продвижения",
        "SELECT operation_key AS ident,amount||' ₽ · '||status AS label FROM promotion_expenses ORDER BY created_at DESC,operation_key",
        None,
    ),
    "evaluations": (
        "История dry-run",
        "SELECT CAST(id AS TEXT) AS ident,item_id AS label FROM promotion_evaluations ORDER BY id DESC",
        "aq:evaluation:",
    ),
}


class AutomationQueue:
    """Secret-free queue inspection. Only local stop/reschedule actions are offered."""

    def __init__(self, store, api):
        self.store, self.access = store, Automation(store, api)

    def listing(self, name, page):
        if name not in LISTS or not 0 <= page <= 1_000_000:
            raise ValidationError("Список или страница недоступны")
        title, query, prefix = LISTS[name]
        values = self.store.rows(query + " LIMIT 11 OFFSET ?", (page * 10,))
        rows = []
        text = f"<b>{title}</b> · страница {page + 1}\n"
        for value in values[:10]:
            label = value["ident"][:35] + " · " + value["label"][:45]
            if prefix:
                rows.append([(label, prefix + value["ident"])])
            else:
                text += "\n" + escaped(label, 200)
        if not values:
            text += "Записей нет."
        navigation = []
        if page:
            navigation.append(("← Назад", f"aq:list:{name}:{page - 1}"))
        if len(values) > 10:
            navigation.append(("Далее →", f"aq:list:{name}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([("Автоматизация", "automation"), ("Продвижение", "promotions")])
        return text, keyboard(rows)

    def chain_snapshot(self, ident):
        rows = self.store.rows(
            "SELECT order_id,item_id,status,policy_revision FROM delivery_chains WHERE order_id=?", (ident,)
        )
        if not rows:
            raise Conflict("Цепочка не найдена")
        steps = self.store.rows(
            "SELECT s.position,s.kind,s.operation_id,o.status,o.error_code FROM delivery_chain_steps s LEFT JOIN outbox o ON o.id=s.operation_id WHERE s.order_id=? ORDER BY s.position",
            (ident,),
        )
        return rows[0], steps, hashlib.sha256(encode([rows[0], steps]).encode()).hexdigest()

    async def callback(self, action, actor, state):
        self.access.orders.owner(actor)
        parts = action.split(":")
        if len(parts) == 4 and parts[1] == "list":
            await state.clear()
            return self.listing(parts[2], int(parts[3]))
        if len(parts) != 3:
            raise ValidationError("Кнопка очереди устарела")
        command, ident = parts[1:]
        if command == "evaluation":
            rows = self.store.rows(
                "SELECT item_id,result,checked_at FROM promotion_evaluations WHERE id=?", (int(ident),)
            )
            if not rows:
                raise Conflict("Проверка не найдена")
            row = rows[0]
            result = json.loads(row["result"])
            return "🔎 <b>Сохранённый dry-run</b>\n" + checked_time(
                row["checked_at"]
            ) + "\nСтоимость: " + escaped(result.get("price") or "неизвестна", 40) + " ₽\nПо политике: " + (
                "допустимо" if result.get("would_allow") else "запрещено"
            ) + "\nПричины: " + escaped(
                "; ".join(result.get("reasons", [])) or "нет", 1800
            ) + "\nПозиция: " + escaped(result.get("position") or "неизвестна", 40) + "\n" + escaped(
                result.get("position_scope") or "", 200
            ) + "\nЭто история проверки, не подтверждение текущих условий или списания.", keyboard(
                [[("Товар", "promo:item:" + row["item_id"]), ("История", "aq:list:evaluations:0")]]
            )
        if command == "target":
            rows = self.store.rows("SELECT * FROM promotion_schedule WHERE item_id=?", (ident,))
            targets = self.store.rows("SELECT disposition FROM promotion_targets WHERE item_id=?", (ident,))
            text = (
                "🚀 <b>Цель продвижения</b>\n"
                + escaped(ident, 100)
                + "\nВыбор: "
                + (
                    {"include": "выбран", "exclude": "исключён"}[targets[0]["disposition"]]
                    if targets
                    else "по общему правилу"
                )
            )
            if rows:
                row = rows[0]
                text += (
                    "\nПоследняя проверка: "
                    + checked_time(row["checked_at"])
                    + f"\nОшибок подряд: {row['failures']}\nКод: "
                    + escaped(row["error_code"] or "нет", 100)
                )
                text += "\nLease: " + (
                    "занят" if row["lease_until"] and row["lease_until"] > time.time() else "свободен"
                )
                text += "\nСрок следующей проверки: " + (
                    checked_time(datetime.fromtimestamp(row["due_at"], UTC).isoformat())
                    if row["due_at"] > 0
                    else "при ближайшем разрешённом цикле"
                )
            return text + "\nПовторная проверка только читает API и не покупает размещение.", keyboard(
                [
                    [("Настроить выбор/проверить", "promo:item:" + ident)],
                    [("Повторить по расписанию", "aq:recheck:" + ident)],
                    [("Очередь", "aq:list:schedule:0")],
                ]
            )
        if command == "recheck":
            await self.access.item(actor, ident)
            with self.store.transaction() as db:
                row = db.execute(
                    "SELECT lease_until FROM promotion_schedule WHERE item_id=?", (ident,)
                ).fetchone()
                if row and row[0] and row[0] > time.time():
                    raise Conflict("Проверка уже выполняется; lease не сбрасывается")
                db.execute(
                    "INSERT INTO promotion_schedule(item_id) VALUES(?) ON CONFLICT(item_id) DO UPDATE SET due_at=0,failures=0,error_code=NULL",
                    (ident,),
                )
                db.execute(
                    "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,'RESCHEDULE_PROMOTION',?,'READ_ONLY',?)",
                    (actor, ident, now()),
                )
            return "Read-only проверка запланирована. Выключенная политика остаётся выключенной.", keyboard(
                [[("Цель", "aq:target:" + ident)]]
            )
        if command in {"chain", "stop"}:
            chain, steps, snapshot = self.chain_snapshot(ident)
            if command == "stop":
                if chain["status"] != "ACTIVE":
                    raise Conflict("Цепочка уже остановлена или завершена")
                nonce = secrets.token_hex(8)
                await state.set_state(None)
                await state.set_data({"chain_stop": {"id": ident, "snapshot": snapshot, "nonce": nonce}})
                return (
                    "Остановить последующие шаги? Уже отправленные сообщения не отзываются. Запрос в полёте может завершиться; его результат будет сохранён. Зарезервированный код останется в карантине.",
                    keyboard([[("⛔ Остановить", "aq:apply:" + nonce)]]),
                )
            text = (
                "⚡ <b>Цепочка выдачи</b>\nЗаказ: "
                + escaped(ident, 100)
                + "\nСостояние: "
                + chain["status"]
                + f"\nПолитика: v{chain['policy_revision']}"
            )
            rows = []
            for step in steps:
                text += f"\n{step['position'] + 1}. {step['kind']} · {step['status'] or 'ещё не поставлен'}"
                if step["error_code"]:
                    text += " · " + escaped(step["error_code"], 80)
                if step["operation_id"]:
                    rows.append(
                        [("Операция шага " + str(step["position"] + 1), "ops:show:" + step["operation_id"])]
                    )
            if chain["status"] == "ACTIVE":
                rows.append([("⛔ Остановить цепочку", "aq:stop:" + ident)])
            if chain["status"] == "NEEDS_FULFILLMENT":
                rows.append([("Проверить выполнение", "ff:start:" + ident)])
            rows.extend(
                [
                    [("Заказ", "order:" + ident), ("Обновить", "aq:chain:" + ident)],
                    [("Цепочки", "aq:list:chains:0")],
                ]
            )
            return text, keyboard(rows)
        if command == "apply":
            data = (await state.get_data()).get("chain_stop", {})
            if data.get("nonce") != ident:
                raise Conflict("Подтверждение остановки устарело")
            with self.store.transaction() as db:
                chain, _, snapshot = self.chain_snapshot(data["id"])
                if chain["status"] != "ACTIVE" or snapshot != data["snapshot"]:
                    raise Conflict("Цепочка изменилась. Проверьте результаты шагов заново")
                db.execute("UPDATE delivery_chains SET status='CANCELLED' WHERE order_id=?", (data["id"],))
                db.execute(
                    "UPDATE inventory SET status='QUARANTINED' WHERE status='RESERVED' AND id=(SELECT inventory_id FROM reservations WHERE order_id=?)",
                    (data["id"],),
                )
                db.execute(
                    "INSERT INTO order_history(order_id,source,new_status,actor,note,created_at) SELECT id,'CHAIN_STOPPED',status,?,'Владелец остановил последующие шаги',? FROM orders WHERE id=?",
                    (actor, now(), data["id"]),
                )
            await state.clear()
            return (
                "Цепочка остановлена. Результаты внешних запросов не изменены; код автоматически в запас не возвращён.",
                keyboard([[("Цепочка", "aq:chain:" + data["id"])]]),
            )
        raise ValidationError("Действие очереди недоступно")
