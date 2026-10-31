"""Nested paid placement screens; confirmation tokens use existing durable FSM."""

import secrets

from app.database.store import Conflict
from app.services.paid_promotions import PaidPromotions
from app.services.products import priority_price_basis
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard


class PaidPromotionsPanel:
    def __init__(self, store, api):
        self.store, self.api, self.service = store, api, PaidPromotions(store, api)

    async def callback(self, action, owner, state):
        self.service.settings.access.orders.owner(owner)
        parts = action.split(":")
        if parts[1] == "item" and len(parts) == 3:
            ident = parts[2]
            item = await self.service.settings.access.item(owner, ident)
            quotes = await self.api.call(
                "get_item_priority_statuses", item_id=ident, item_price=priority_price_basis(item)
            )
            choices = [q for q in quotes if q.get("type") == "PREMIUM" and q.get("price") is not None]
            nonce = secrets.token_hex(6)
            await state.clear()
            await state.set_data(
                {"paid_choices": {"nonce": nonce, "item_id": ident, "ids": [q["id"] for q in choices]}}
            )
            return "⭐ <b>PREMIUM-размещение</b>\n" + escaped(
                item.get("name") or "Товар", 200
            ) + "\nВыберите актуальный тариф. Перед списанием будет подтверждение.", keyboard(
                [
                    [(f"PREMIUM · {q['price']} ₽ · {q.get('period', '?')} дн.", f"payp:pick:{nonce}:{i}")]
                    for i, q in enumerate(choices)
                ]
                + [[("Лимиты расходов / продвижение", "promotions")]],
                back=("⬅️ К товару", "item:" + ident),
            )
        if parts[1] == "pick" and len(parts) == 4:
            view = (await state.get_data()).get("paid_choices", {})
            if view.get("nonce") != parts[2] or not 0 <= int(parts[3]) < len(view.get("ids", [])):
                raise Conflict("Тариф устарел. Откройте товар заново")
            value = await self.service.preview(owner, view["item_id"], view["ids"][int(parts[3])])
            nonce = secrets.token_hex(8)
            await state.set_data({"paid_pending": {"nonce": nonce, "value": value}})
            verb = (
                "Опубликовать PREMIUM"
                if value["item"]["status"] == "DRAFT"
                else "Поднять / повысить до PREMIUM"
            )
            text = (
                "💳 <b>Подтверждение расхода</b>\n"
                + escaped(value["name"], 200)
                + f"\n{verb}: {value['amount']} ₽ с баланса Playerok."
                + "\nЛимиты берутся из настроек продвижения. Тариф повторно проверяется перед отправкой."
                + "\nНеизвестный результат блокирует повторное списание."
            )
            rows = [[("✅ Подтвердить расход " + value["amount"] + " ₽", "payp:commit:" + nonce)]]
            if value["item"]["status"] == "APPROVED" and value["item"]["priority"] == "PREMIUM":
                rows.append([("Разрешить этот тариф для автоподнятия", "payp:approve:" + nonce)])
                text += (
                    "\nРазрешение тарифа добавит товар в выбранные; платное расписание включается отдельно."
                )
            return text, keyboard(rows, back=("Отмена", "payp:item:" + value["item"]["id"]))
        if parts[1] == "arm":
            revision, policy, _selection = self.service.settings.get()
            nonce = secrets.token_hex(8)
            await state.clear()
            await state.set_data(
                {"paid_arm": {"nonce": nonce, "revision": revision, "account": self.api.account.id}}
            )
            return (
                "⚠️ <b>Разрешить платное автоподнятие?</b>"
                + f"\nОперация до {policy.per_operation} ₽; сутки {policy.daily} ₽; месяц {policy.monthly} ₽."
                + f"\nНа товар не более {policy.max_operations_per_item} операций."
                + "\nТолько явно подтверждённые PREMIUM-тарифы выбранных товаров по сохранённому расписанию."
                + "\nНачнутся реальные расходы с баланса Playerok. Изменение настроек потребует нового разрешения.",
                keyboard(
                    [[("✅ Разрешаю расходы по этим лимитам", "payp:confirmarm:" + nonce)]],
                    back=("Отмена", "promotions"),
                ),
            )
        if parts[1] == "confirmarm" and len(parts) == 3:
            p = (await state.get_data()).get("paid_arm", {})
            if p.get("nonce") != parts[2] or p.get("account") != self.api.account.id:
                raise Conflict("Подтверждение расходов устарело")
            self.service.arm(owner, p["revision"])
            await state.clear()
            return "Платное расписание разрешено для текущих настроек и подтверждённых тарифов.", keyboard(
                [[("⛔ Остановить продвижение", "promo:stop")]], back=("К продвижению", "promotions")
            )
        if parts[1] in {"commit", "approve"} and len(parts) == 3:
            p = (await state.get_data()).get("paid_pending", {})
            if p.get("nonce") != parts[2]:
                raise Conflict("Подтверждение устарело или уже использовано")
            if parts[1] == "approve":
                await self.service.approve(owner, p["value"])
                await self.service.settings.target(owner, p["value"]["item"]["id"], "include")
                await state.clear()
                return (
                    "Тариф автоподнятия подтверждён. Само платное расписание включается отдельно.",
                    keyboard(
                        [[("Настройки и включение", "promotions")]],
                        back=("К товару", "item:" + p["value"]["item"]["id"]),
                    ),
                )
            op = await self.service.queue(owner, p["value"], "paid-ui:" + parts[2])
            await state.clear()
            return (
                "⏳ Платное действие ожидает выполнения. Успех будет показан только после сверки платежа и товара.",
                keyboard(
                    [[("Проверить результат", "result:show:" + op)]],
                    back=("К товару", "item:" + p["value"]["item"]["id"]),
                ),
            )
        raise Conflict("Кнопка платного размещения устарела")
