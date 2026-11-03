"""Nested paid placement screens; confirmation tokens use existing durable FSM."""

import secrets

from app.database.store import Conflict
from app.services.finance import money
from app.services.paid_promotions import PaidPromotions, placement_funds
from app.services.products import priority_price_basis
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard


class PaidPromotionsPanel:
    def __init__(self, store, api):
        self.store, self.api, self.service = store, api, PaidPromotions(store, api)

    async def draft_launch(self, owner, ident, state, quote_id=None):
        from app.telegram.gifts_panel import GiftsPanel
        from app.telegram.launch_panel import LaunchPanel

        panel = LaunchPanel(self.store, self.api, GiftsPanel(self.store, self.api).service)
        launch_id = await panel.service.open(owner, ident)
        draft = panel.service.load(owner, launch_id)
        if draft["state"] == "DRAFT":
            value = draft["payload"]
            value.update(publication="PREMIUM", quote_id=quote_id, ready=None)
            self.store.update_draft(launch_id, owner, draft["revision"], value)
        return await panel.show(owner, state, launch_id)

    async def callback(self, action, owner, state):
        self.service.settings.access.orders.owner(owner)
        parts = action.split(":")
        if parts[1] == "item" and len(parts) == 3:
            ident = parts[2]
            item = await self.service.settings.access.item(owner, ident)
            if item.get("status") == "DRAFT":
                return await self.draft_launch(owner, ident, state)
            quotes = await self.api.call(
                "get_item_priority_statuses", item_id=ident, item_price=priority_price_basis(item)
            )
            choices = [q for q in quotes if q.get("type") == "PREMIUM" and q.get("price") is not None]
            nonce = secrets.token_hex(6)
            await state.clear()
            await state.set_data(
                {"paid_choices": {"nonce": nonce, "item_id": ident, "ids": [q["id"] for q in choices]}}
            )
            _revision, _policy, selection = self.service.settings.get()
            selected = self.service.settings.selected(ident, selection)
            priority = item.get("priority")
            priority_type = priority.get("type") if isinstance(priority, dict) else priority
            return "⭐ <b>Продвижение товара</b>\n" + escaped(item.get("name") or "Товар", 200) + (
                "\nPREMIUM: установлен" if priority_type == "PREMIUM" else "\nPREMIUM: не подтверждён"
            ) + (
                "\nВ расписании: выбран" if selected else "\nВ расписании: не выбран"
            ) + "\nВыберите актуальный тариф. Перед списанием будет подтверждение." + (
                "" if choices else "\nСейчас сервер не предложил доступных PREMIUM-тарифов."
            ), keyboard(
                [
                    [(f"PREMIUM · {q['price']} ₽ · {q.get('period', '?')} дн.", f"payp:pick:{nonce}:{i}")]
                    for i, q in enumerate(choices)
                ]
                + [
                    [("Расписание этого товара", "promo:item:" + ident)],
                    [("Лимиты и автопродвижение", "promotions")],
                ],
                back=("⬅️ К товару", "item:" + ident),
            )
        if parts[1] == "pick" and len(parts) == 4:
            view = (await state.get_data()).get("paid_choices", {})
            if view.get("nonce") != parts[2] or not 0 <= int(parts[3]) < len(view.get("ids", [])):
                raise Conflict("Тариф устарел. Откройте товар заново")
            value = await self.service.preview(owner, view["item_id"], view["ids"][int(parts[3])])
            if value["item"]["status"] == "DRAFT":
                return await self.draft_launch(owner, view["item_id"], state, value["quote_id"])
            viewer = await self.api.call("get")
            available, pending, capacity = placement_funds(viewer)
            if viewer.get("id") != value["account_id"] or available is None:
                raise Conflict("Не удалось подтвердить аккаунт и доступный баланс Playerok")
            reserved = sum(
                (
                    money(row["amount"])
                    for row in self.store.rows(
                        "SELECT amount FROM promotion_expenses WHERE status='RESERVED'"
                    )
                ),
                money(0),
            )
            if capacity < reserved + money(value["amount"]):
                raise Conflict(
                    "Недостаточно баланса Playerok или средств в заморозке с учётом резервов; источники не складываются"
                )
            if self.store.rows("SELECT 1 FROM promotion_expenses WHERE status='UNCERTAIN' LIMIT 1"):
                raise Conflict("Предыдущая оплата требует сверки. Новая покупка пока заблокирована")
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
                + f"\nБаланс: {available} ₽ · заморозка дохода: {pending} ₽."
                + "\nЕсли баланса недостаточно, Playerok может оплатить размещение из заморозки. Источники не складываются. Разовая покупка, расписание не включается."
                + "\nВаше согласие ограничено этой суммой и тарифом; перед оплатой проверим их заново."
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
            if p["value"]["item"]["status"] == "DRAFT":
                return await self.draft_launch(owner, p["value"]["item"]["id"], state, p["value"]["quote_id"])
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
            op = await self.service.queue(owner, p["value"], "paid-ui:" + parts[2], manual=True)
            await state.clear()
            return (
                "⏳ Платное действие ожидает выполнения. Успех будет показан только после сверки платежа и товара.",
                keyboard(
                    [[("Проверить результат", "result:show:" + op)]],
                    back=("К товару", "item:" + p["value"]["item"]["id"]),
                ),
            )
        raise Conflict("Кнопка платного размещения устарела")
