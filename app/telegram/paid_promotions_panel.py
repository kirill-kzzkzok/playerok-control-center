"""Nested paid placement screens; confirmation tokens use existing durable FSM."""

import json
import secrets

from app.database.store import Conflict
from app.playerok.adapter import APIError
from app.services.finance import money
from app.services.paid_promotions import PaidPromotions, placement_funds
from app.services.premium_review import PremiumReview, blocking_expenses
from app.services.products import priority_price_basis
from app.services.reconciliation import signature
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.presentation import local_time


class PaidPromotionsPanel:
    def __init__(self, store, api):
        self.store, self.api, self.service = store, api, PaidPromotions(store, api)
        self.review = PremiumReview(store, api)

    async def blocked(self, ident):
        pending = blocking_expenses(self.store)
        if not pending:
            return None
        row = pending[0]
        payload = json.loads(row["payload"] or "{}")
        text = (
            "⛔ <b>Новая покупка пока заблокирована</b>\n"
            + escaped(payload.get("name") or "Товар", 150)
            + f" · {money(row['amount']):.2f} ₽ · {local_time(row['started_at'] or row['created_at'])}"
            + "\nРезультат прежнего запроса неизвестен."
        )
        try:
            debit = await self.review.observe(row)
        except Conflict as error:
            text += "\n" + escaped(str(error), 200)
        except (APIError, ValueError):
            text += "\nОднозначное списание в истории сейчас не установлено."
        else:
            text += (
                f"\nВ истории подтверждено списание {money(debit['amount']):.2f} ₽ · {local_time(debit['at'])}."
                " Связь списания с этим лотом не доказана."
            )
        intent = self.store.setting("promotions:intent:" + str(row["id"]), {})
        if intent.get("candidate_id") and intent["candidate_id"] == payload.get("before_payment_id"):
            text += "\nПосле запроса Playerok вернул ID предыдущего платежа."
        rows = [[("Открыть операцию", "ops:show:" + row["id"])]] if row["id"] else []
        if len(pending) > 1:
            text += f"\nТаких операций: {len(pending)}."
        return text, keyboard(rows, back=("⬅️ К товару", "item:" + ident))

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
        if parts[1] == "review" and len(parts) == 3:
            ident = parts[2]
            row = next((r for r in blocking_expenses(self.store) if r["id"] == ident), None)
            if not row:
                raise Conflict("Операция уже рассмотрена или недоступна")
            evidence = await self.review.observe(row)
            nonce = secrets.token_hex(8)
            await state.set_data(
                {
                    "paid_review": {
                        "nonce": nonce,
                        "id": ident,
                        "target": row["target"],
                        "signature": signature(row),
                        "evidence": evidence,
                        "revision": self.service.settings.get()[0],
                    }
                }
            )
            return (
                "💳 <b>Финансовое решение</b>\n"
                + escaped(json.loads(row["payload"]).get("name") or "Товар", 150)
                + f"\nПодтверждённое списание в кошельке: {money(evidence['amount']):.2f} ₽ · {local_time(evidence['at'])}."
                + "\nСвязь с этим лотом не доказана. Исход старой операции останется неизвестным, её сумма будет учтена в расходах."
                + "\nСтарый запрос не повторится. Новые покупки потребуют отдельного согласия. Автоподнятие будет выключено.",
                keyboard(
                    [[("Учесть расход и разрешить новые покупки", "payp:applyreview:" + nonce)]],
                    back=("Отмена", "ops:show:" + ident),
                ),
            )
        if parts[1] == "applyreview" and len(parts) == 3:
            view = (await state.get_data()).get("paid_review", {})
            if parts[2] != view.get("nonce"):
                raise Conflict("Финансовое решение устарело")
            await self.review.approve(
                owner, view["id"], view["signature"], view["evidence"], view["revision"]
            )
            await state.clear()
            return (
                "Расход учтён. Старый запрос не повторится, его исход неизвестен. Автоподнятие выключено.",
                keyboard([[("К продвижению товара", "payp:item:" + view["target"])]]),
            )
        if parts[1] in {"item", "auto"} and len(parts) == 3:
            ident = parts[2]
            blocked = await self.blocked(ident)
            if blocked:
                return blocked
            auto = parts[1] == "auto"
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
                {
                    "paid_choices": {
                        "nonce": nonce,
                        "item_id": ident,
                        "ids": [q["id"] for q in choices],
                        "prices": [str(money(q["price"])) for q in choices],
                        "auto": auto,
                    }
                }
            )
            _revision, policy, selection = self.service.settings.get()
            selected = self.service.settings.selected(ident, selection)
            priority = item.get("priority")
            priority_type = priority.get("type") if isinstance(priority, dict) else priority
            if auto:
                from app.telegram.promotions_panel import buying_conditions

                return "🚀 <b>Автоподнятие</b>\n" + buying_conditions(policy) + (
                    "\nТовар выбран." if selected else "\nТовар пока не выбран."
                ) + "\nПодтвердите тариф и разрешите повторные расходы в настройках автоподнятия.", keyboard(
                    [
                        [(f"Разрешить тариф до {q['price']} ₽", f"payp:pick:{nonce}:{i}")]
                        for i, q in enumerate(choices)
                    ]
                    + [[("Условия и включение", "promotions")]],
                    back=("К продвижению", "payp:item:" + ident),
                )
            return "⭐ <b>Продвижение товара</b>\n" + escaped(item.get("name") or "Товар", 200) + (
                "\nPREMIUM: установлен" if priority_type == "PREMIUM" else "\nPREMIUM: не подтверждён"
            ) + ("\nАвтоподнятие: товар выбран" if selected else "\nАвтоподнятие: товар не выбран") + (
                "" if choices else "\nСейчас сервер не предложил доступных PREMIUM-тарифов."
            ), keyboard(
                [
                    [
                        (
                            f"Поднять за {q['price']} ₽"
                            + (f" · {q.get('period', '?')} дн." if len(choices) > 1 else ""),
                            f"payp:pick:{nonce}:{i}",
                        )
                    ]
                    for i, q in enumerate(choices)
                ]
                + [
                    [("Автоподнятие", "payp:auto:" + ident)],
                ],
                back=("⬅️ К товару", "item:" + ident),
            )
        if parts[1] == "pick" and len(parts) == 4:
            view = (await state.get_data()).get("paid_choices", {})
            if view.get("nonce") != parts[2] or not 0 <= int(parts[3]) < len(view.get("ids", [])):
                raise Conflict("Тариф устарел. Откройте товар заново")
            blocked = await self.blocked(view["item_id"])
            if blocked:
                return blocked
            value = await self.service.preview(owner, view["item_id"], view["ids"][int(parts[3])])
            shown = (view.get("prices") or [None] * len(view["ids"]))[int(parts[3])]
            changed_price = (
                "\nPlayerok обновил цену. Ниже показана новая сумма."
                if shown and shown != value["amount"]
                else ""
            )
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
            nonce = secrets.token_hex(8)
            await state.set_data(
                {"paid_pending": {"nonce": nonce, "value": value, "auto": view.get("auto", False)}}
            )
            if view.get("auto"):
                return (
                    f"Разрешить использовать тариф {value['amount']} ₽ для автоподнятия? Это сохранит цену тарифа, без покупки сейчас."
                    + changed_price,
                    keyboard(
                        [[("Разрешить тариф", "payp:approve:" + nonce)]],
                        back=("Отмена", "payp:auto:" + view["item_id"]),
                    ),
                )
            verb = (
                "Опубликовать PREMIUM"
                if value["item"]["status"] == "DRAFT"
                else "Поднять / повысить до PREMIUM"
            )
            text = (
                "💳 <b>Подтверждение расхода</b>\n"
                + escaped(value["name"], 200)
                + f"\n{verb}: {value['amount']} ₽ с баланса Playerok."
                + changed_price
                + f"\nБаланс: {available} ₽ · заморозка дохода: {pending} ₽."
                + "\nРазовая покупка. Playerok может использовать доступные средства или замороженный доход."
                + "\nВаше согласие ограничено этой суммой и тарифом; перед оплатой проверим их заново."
                + "\nНеизвестный результат блокирует повторное списание."
            )
            rows = [[("✅ Подтвердить расход " + value["amount"] + " ₽", "payp:commit:" + nonce)]]
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
                + "\nБот покупает поднятие только для выбранных товаров и по разрешённым тарифам."
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
            return "Повторные покупки разрешены для текущих настроек и подтверждённых тарифов.", keyboard(
                [[("⛔ Остановить продвижение", "promo:stop")]], back=("К продвижению", "promotions")
            )
        if parts[1] in {"commit", "approve"} and len(parts) == 3:
            p = (await state.get_data()).get("paid_pending", {})
            if p.get("nonce") != parts[2]:
                raise Conflict("Подтверждение устарело или уже использовано")
            blocked = await self.blocked(p["value"]["item"]["id"])
            if blocked:
                return blocked
            if p.get("auto") and parts[1] == "commit":
                raise Conflict("Разрешение тарифа не подтверждает разовую покупку")
            if p["value"]["item"]["status"] == "DRAFT":
                return await self.draft_launch(owner, p["value"]["item"]["id"], state, p["value"]["quote_id"])
            if parts[1] == "approve":
                await self.service.approve(owner, p["value"])
                await self.service.settings.target(owner, p["value"]["item"]["id"], "include")
                await state.clear()
                return (
                    "Тариф автоподнятия подтверждён. Автоподнятие включается отдельно.",
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
