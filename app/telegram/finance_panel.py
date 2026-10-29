import secrets
from html import escape

from app.database.store import now
from app.playerok.adapter import APIError
from app.services.finance import format_calculation, money
from app.services.financial_quotes import save_quote
from app.services.products import priority_price_basis
from app.services.withdrawals import (
    draft_calculation,
    estimate_withdrawal,
    format_withdrawal,
    withdrawal_methods,
    withdrawal_snapshot,
)
from app.telegram.keyboards import keyboard


class FinancePanel:
    """Only read API and local settings; no money-moving method is exposed."""

    def __init__(self, store, api, products):
        self.store, self.api, self.products = store, api, products

    def handles(self, action):
        return action == "providers" or action.startswith(("fin:", "finance:"))

    def preference_key(self, owner):
        return f"finance:{owner}:withdrawal"

    async def read_owned(self, ident):
        item = await self.api.call("get_item", id=ident)
        if (
            not getattr(self.api.account, "id", None)
            or (item.get("user") or {}).get("id") != self.api.account.id
        ):
            raise ValueError("Владение товаром не подтверждено")
        return item

    async def item(self, owner, ident):
        item = await self.read_owned(ident)
        payload = {
            "price": item["price"],
            "fee_multiplier": item.get("fee_multiplier"),
            "fee_checked_at": now(),
            "cost": self.store.setting(f"finance:{owner}:cost:{ident}"),
        }
        chosen = self.store.setting(self.preference_key(owner))
        if chosen:
            payload["withdrawal"] = await withdrawal_snapshot(self.api, chosen["provider"]["id"])
        placement = None
        if item.get("status") == "DRAFT" and item.get("raw_price") is not None:
            try:
                quotes = await self.products.priority_preview(ident, priority_price_basis(item))
            except APIError:
                quotes = []
            preferred = self.store.setting(f"finance:{owner}:placement:{ident}")
            chosen = (
                next((q for q in quotes if q.get("id") == preferred), None)
                if preferred
                else next(
                    (
                        q
                        for q in quotes
                        if q.get("type") == "DEFAULT"
                        and q.get("price") is not None
                        and money(q["price"]) == 0
                    ),
                    None,
                )
            )
            if chosen and chosen.get("price") is not None:
                placement = money(chosen["price"])
                payload["placement_quote"] = {
                    "id": chosen["id"],
                    "type": chosen.get("type"),
                    "price": str(placement),
                    "base_price": priority_price_basis(item),
                    "checked_at": now(),
                    "source": "Playerok itemPriorityStatuses",
                }
        result = draft_calculation(payload, placement=placement)
        save_quote(self.store, owner, "item:" + ident, {**payload, "placement": placement}, result)
        name = ((payload.get("withdrawal") or {}).get("provider") or {}).get("name") or "не выбран"
        text = (
            f"📦 <b>{escape(item.get('name') or ident)}</b>\n\n{escape(format_calculation(result))}\n\n"
            f"Способ вывода: {escape(name)}\n"
            "Минимальная комиссия рассчитана для вывода суммы этой продажи отдельной операцией. При объединении продаж результат меняется. Для USDT показан рублёвый эквивалент, не сумма токенов.\n"
            "Платное размещение не покупается и не вычитается повторно из следующих продаж."
        )
        return text, keyboard(
            [
                [("💳 Способ вывода", "providers")],
                [("⭐ Стоимость нового размещения", "fin:placement:" + ident)],
                [("✏️ Себестоимость", "fin:cost:" + ident)],
                [("Очистить себестоимость", "fin:clearcost:" + ident)],
                [("🔄 Обновить расчёт", "finance:" + ident)],
                [("⬅️ Товар", "item:" + ident)],
            ]
        )

    async def callback(self, action, owner, state):
        if action == "providers":
            methods = await withdrawal_methods(self.api)
            await state.set_state(None)
            await state.set_data({"finance_methods": methods})
            text = "💳 <b>Способы вывода · только расчёт</b>\n" + "\n".join(
                f"{escape(value.get('name') or value['id'])}: {value.get('fee')}%, минимум комиссии {value.get('min_fee_amount') if value.get('min_fee_amount') is not None else 'не задан'} ₽"
                for value in methods
            )
            text += "\n\nВыберите способ для оценки. Никаких средств бот не выводит. Для USDT дополнительно учитывается 1 USDT по актуальному курсу."
            return text, keyboard(
                [[(str(value.get("name") or value["id"]), "fin:select:" + value["id"])] for value in methods]
            )
        if action.startswith("fin:select:"):
            ident = action.split(":", 2)[2]
            data = await state.get_data()
            if ident not in {value["id"] for value in data.get("finance_methods", [])}:
                raise ValueError("Кнопка выбора способа устарела; откройте список заново")
            snapshot = await withdrawal_snapshot(self.api, ident)
            self.store.set_setting(self.preference_key(owner), snapshot)
            await state.set_state("finance:amount")
            await state.set_data({"withdrawal": snapshot})
            return (
                "Введите сумму предполагаемого вывода в рублях. Это только калькулятор; средства останутся на месте.",
                keyboard([[("Сменить способ", "providers")]]),
            )
        if action == "fin:refresh":
            data = await state.get_data()
            chosen = data.get("withdrawal") or self.store.setting(self.preference_key(owner))
            if not chosen:
                raise ValueError("Сначала выберите способ вывода")
            snapshot = await withdrawal_snapshot(self.api, chosen["provider"]["id"])
            self.store.set_setting(self.preference_key(owner), snapshot)
            await state.update_data(withdrawal=snapshot)
            if data.get("withdrawal_amount") is not None:
                return self.amount_text(data["withdrawal_amount"], snapshot, owner)
            await state.set_state("finance:amount")
            return "Параметры обновлены. Введите предполагаемую сумму вывода в рублях.", keyboard([])
        if action.startswith("finance:"):
            await state.clear()
            return await self.item(owner, action.split(":", 1)[1])
        if action.startswith("fin:cost:"):
            ident = action.split(":", 2)[2]
            await state.set_state("finance:cost")
            await state.set_data({"finance_item_id": ident})
            return (
                "Введите себестоимость одной единицы в рублях. Значение сохранится локально и не изменит товар на Playerok.",
                keyboard([]),
            )
        if action.startswith("fin:placement:"):
            ident = action.split(":", 2)[2]
            item = await self.read_owned(ident)
            if item.get("status") != "DRAFT":
                raise ValueError(
                    "Разовую стоимость нового размещения можно включить в расчёт серверного черновика. Прежние расходы не вычитаются повторно."
                )
            options = await self.products.priority_preview(ident, priority_price_basis(item))
            nonce = secrets.token_hex(6)
            await state.set_state(None)
            await state.set_data(
                {"placement_item_id": ident, "placement_options": options, "placement_token": nonce}
            )
            return (
                "⭐ Выберите стоимость для предварительного расчёта нового размещения. Это не публикация и не покупка PREMIUM.",
                keyboard(
                    [
                        [
                            (
                                f"{value.get('name') or value.get('type')}: {value.get('price', '?')} ₽",
                                f"fin:place:{nonce}:{i}",
                            )
                        ]
                        for i, value in enumerate(options)
                    ]
                ),
            )
        if action.startswith("fin:place:"):
            _, _, nonce, raw_index = action.split(":")
            data = await state.get_data()
            index = int(raw_index)
            if nonce != data.get("placement_token") or not 0 <= index < len(
                data.get("placement_options", [])
            ):
                raise ValueError("Предварительный выбор размещения устарел")
            option = data["placement_options"][index]
            ident = data["placement_item_id"]
            self.store.set_setting(f"finance:{owner}:placement:{ident}", option["id"])
            await state.clear()
            return await self.item(owner, ident)
        if action.startswith("fin:clearcost:"):
            ident = action.split(":", 2)[2]
            self.store.execute("DELETE FROM settings WHERE key=?", (f"finance:{owner}:cost:{ident}",))
            await state.clear()
            return await self.item(owner, ident)
        raise ValueError("Неизвестное действие финансового калькулятора")

    def amount_text(self, amount, snapshot, owner):
        estimate = estimate_withdrawal(amount, snapshot)
        save_quote(
            self.store,
            owner,
            "withdrawal",
            {"withdrawal_amount": str(estimate.amount), "withdrawal": snapshot},
            estimate,
        )
        return escape(
            format_withdrawal(estimate, snapshot["provider"].get("name") or snapshot["provider"]["id"])
        ) + "\n\nВведите другую сумму или обновите параметры. Вывод не выполняется.", keyboard(
            [[("🔄 Обновить условия", "fin:refresh")], [("Сменить способ", "providers")]]
        )

    async def message(self, message, state):
        current = await state.get_state()
        data = await state.get_data()
        amount = money((message.text or "").replace(",", "."))
        if current == "finance:amount":
            if not data.get("withdrawal"):
                raise ValueError("Способ вывода не выбран")
            await state.update_data(withdrawal_amount=str(amount))
            return self.amount_text(amount, data["withdrawal"], message.from_user.id)
        if current == "finance:cost":
            owner, ident = message.from_user.id, data["finance_item_id"]
            # Read ownership before storing a product-specific preference.
            await self.read_owned(ident)
            self.store.set_setting(f"finance:{owner}:cost:{ident}", str(amount))
            await state.clear()
            return await self.item(owner, ident)
        raise ValueError("Финансовый сценарий устарел")
