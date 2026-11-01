import json
import secrets
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from html import escape

from app.database.store import now
from app.playerok.adapter import APIError
from app.services.finance import FeeRule, money, selected_fee_multiplier
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

    async def item(self, owner, ident, *, details=False):
        item = await self.read_owned(ident)
        payload = {
            "price": item["price"],
            "fee_multiplier": item.get("fee_multiplier"),
            "fee_checked_at": now(),
            "cost": self.store.setting(f"finance:{owner}:cost:{ident}"),
        }
        obtaining = item.get("obtaining_type") or {}
        payload["fee_multiplier"] = selected_fee_multiplier(
            item.get("fee_multiplier"), obtaining.get("fee_multiplier")
        )
        payload["fee_basis"] = {
            "item_id": ident,
            "obtaining_type_id": obtaining.get("id"),
            "category_id": (item.get("category") or {}).get("id"),
        }
        if (
            item.get("fee_multiplier") is None
            and obtaining.get("fee_multiplier") is None
            and payload["fee_basis"]["category_id"]
        ):
            try:
                schema = await self.products.schema_details(
                    payload["fee_basis"]["category_id"], obtaining.get("id"), auto_agreements=False
                )
                payload["fee_multiplier"] = schema["fee_multiplier"]
                payload["fee_checked_at"] = schema["checked_at"]
            except APIError:
                payload["fee_multiplier"] = None
        chosen = self.store.setting(self.preference_key(owner))
        if chosen:
            payload["withdrawal"] = await withdrawal_snapshot(self.api, chosen["provider"]["id"])
        placement, quotes = None, []
        if item.get("raw_price") is not None:
            try:
                quotes = await self.products.priority_preview(ident, priority_price_basis(item))
            except APIError:
                quotes = []
        if item.get("status") == "DRAFT" and quotes:
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
        allocation = self.store.setting(f"finance:{owner}:placement_per_sale:{ident}")
        result = draft_calculation(payload, placement=money(allocation) if allocation is not None else None)
        save_quote(self.store, owner, "item:" + ident, {**payload, "placement": allocation}, result)
        name = ((payload.get("withdrawal") or {}).get("provider") or {}).get("name") or "не выбран"

        def rub(value):
            return str(value) + " ₽" if value is not None else "неизвестно"

        net = result.after_withdrawal
        cost = result.cost
        profit = result.profit
        text = (
            f"💰 <b>{escape(item.get('name') or 'Товар')}</b>\nПрогноз будущей продажи · оценка\n"
            f"Цена продажи: {rub(result.sale)}\nКомиссия Playerok: {rub(result.platform_fee)}\n"
            f"После комиссии площадки: {rub(result.after_platform)}\n"
            f"Комиссия вывода ({escape(name)}): {rub(result.withdrawal_fee)}\n"
            f"На руки после вывода: {rub(net)}\n"
            f"PREMIUM / поднятия на продажу: {rub(money(allocation) if allocation is not None else None)}\n"
            f"Себестоимость: {rub(cost)}\nЧистая прибыль: {rub(profit)}"
        )
        binding = self.store.rows("SELECT approved_stars FROM gift_listings WHERE item_id=?", (ident,))
        if binding:
            text += f"\nПодарки: {binding[0]['approved_stars']} Stars по привязке; RUB-курс не подставляется автоматически."
        if allocation is None:
            text += "\nРазовые расходы не вычтены: распределение не задано."
        if placement is not None:
            text += f"\nНовое размещение (разово, ещё не оплачено): {placement} ₽. В расход каждой продажи автоматически не включается."
        if payload.get("withdrawal") and result.after_platform is not None and result.withdrawal_fee is None:
            quote = estimate_withdrawal(result.after_platform, payload["withdrawal"])
            text += "\n" + escape("; ".join(quote.unknown))
            if quote.minimum_amount and 0 < result.after_platform < quote.minimum_amount:
                count = int(
                    (quote.minimum_amount / result.after_platform).to_integral_value(rounding=ROUND_CEILING)
                )
                batch = estimate_withdrawal(result.after_platform * Decimal(count), payload["withdrawal"])
                if batch.net is not None:
                    text += f"\nПри объединении {count} таких продаж: на руки ≈ {batch.net} ₽ суммарно."
        text += "\nИсторическая прибыль по завершённым заказам учитывается отдельно."
        if not details:
            text = self.compact(owner, ident, item, payload, result, quotes)
        rows = [
            [("Подробности", "fin:details:" + ident)]
            if not details
            else [("К краткому расчёту", "finance:" + ident)],
            [("⚙️ Параметры расчёта", "fin:options:" + ident)],
            [("🔄 Обновить расчёт", "finance:" + ident)],
        ]
        if not payload.get("withdrawal"):
            rows.insert(0, [("💳 Выбрать способ получения денег", "providers")])
        return text, keyboard(rows, back=("⬅️ Товар", "item:" + ident))

    def compact(self, owner, ident, item, payload, result, quotes):
        def rub(value, missing="не получена"):
            return str(value) + " ₽" if value is not None else missing

        lines = [
            "💰 <b>" + escape(item.get("name") or "Товар") + "</b>",
            "Прогноз будущей продажи · оценка",
            "Цена: " + rub(result.sale),
            "Комиссия Playerok: " + rub(result.platform_fee),
            "Себестоимость: " + rub(result.cost, "не задана"),
        ]
        snapshot = payload.get("withdrawal") or {}
        provider = snapshot.get("provider") or {}
        name = "СБП" if provider.get("id") == "SBP" else provider.get("name") or "вывода"
        fee, approximate_part = result.withdrawal_fee, False
        if provider.get("id") == "SBP" and result.after_platform is not None and fee is None:
            quote = estimate_withdrawal(result.after_platform, snapshot)
            if quote.minimum_amount and 0 <= result.after_platform < quote.minimum_amount:
                try:
                    rate = Decimal(str(provider.get("fee")))
                    if rate.is_finite() and 0 <= rate <= 100:
                        fee = FeeRule(
                            rate,
                            Decimal(0),
                            snapshot["source"],
                            snapshot["checked_at"],
                            "SBP percentage only",
                            estimated=True,
                            net_floor_cents=True,
                        ).charge(result.after_platform)
                        approximate_part = True
                except (ValueError, InvalidOperation, KeyError):
                    fee = None
        lines.append(
            "Оценка комиссии "
            + escape(name)
            + ": "
            + (
                "≈ " + rub(fee)
                if fee is not None
                else "выберите способ"
                if not provider
                else "обновите условия"
            )
        )
        if result.after_withdrawal is not None:
            lines.append("На руки после вывода: " + rub(result.after_withdrawal))
        premium = [q for q in quotes if q.get("type") == "PREMIUM" and q.get("price") is not None]
        priority = item.get("priority")
        premium_status = (
            "включён"
            if priority == "PREMIUM"
            else "не включён"
            if priority == "DEFAULT"
            else "статус не подтверждён"
        )
        tariffs = sorted({money(q["price"]) for q in premium})
        tariff = " / ".join(rub(v) for v in tariffs) if tariffs else "не получен"
        lines.append("PREMIUM: " + premium_status + " · новый тариф: " + tariff)
        confirmed = Decimal(0)
        for row in self.store.rows(
            "SELECT e.amount,o.result FROM promotion_expenses e JOIN outbox o ON o.operation_key=e.operation_key WHERE e.item_id=? AND e.status='SUCCEEDED' AND o.status='SUCCEEDED' AND o.kind IN ('premium_publish','premium_bump')",
            (ident,),
        ):
            receipt = json.loads(row["result"] or "null") or {}
            if (
                receipt.get("payment_id")
                and receipt.get("paid_rub") is not None
                and money(receipt["paid_rub"]) == money(row["amount"])
            ):
                confirmed += money(row["amount"])
        if confirmed:
            lines.append("Ранее оплачено PREMIUM/поднятий: " + rub(confirmed) + " разово")
        if result.profit is not None and not result.estimated:
            lines.append("Чистая прибыль: " + rub(result.profit))
        elif result.after_platform is not None and result.cost is not None:
            lines.append("Прибыль до дополнительных расходов: " + rub(result.after_platform - result.cost))
            if fee is not None:
                label = (
                    "Оценка прибыли после процентной комиссии"
                    if approximate_part
                    else "Оценка прибыли после вывода"
                )
                value = result.after_platform - result.cost - fee
                if result.placement_expense is not None:
                    value -= result.placement_expense
                lines.append(label + ": ≈ " + rub(value))
                if result.placement_expense is None:
                    lines.append("Без распределения расходов размещения")
        else:
            lines.append("Для прибыли укажите себестоимость и проверьте комиссию")
        if approximate_part:
            lines.append(
                "Ниже минимума вывода: это процентная часть; фактическая комиссия зависит от суммы и минимальной комиссии."
            )
        return "\n".join(lines)

    async def callback(self, action, owner, state):
        if action.startswith("fin:details:"):
            return await self.item(owner, action.split(":", 2)[2], details=True)
        if action.startswith("fin:options:"):
            ident = action.split(":", 2)[2]
            await self.read_owned(ident)
            return (
                "⚙️ <b>Параметры прогноза</b>\nСебестоимость и распределение расходов задаются для этого товара. Способ получения денег общий для магазина. Исторические продажи не пересчитываются.",
                keyboard(
                    [
                        [("💳 Способ получения денег", "providers")],
                        [("✏️ Себестоимость", "fin:cost:" + ident)],
                        [("Распределить расходы размещения", "fin:allocation:" + ident)],
                        [("Очистить себестоимость", "fin:clearcost:" + ident)],
                        [("⭐ Цена нового размещения", "fin:placement:" + ident)],
                    ],
                    back=("К расчёту", "finance:" + ident),
                ),
            )
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
            await state.set_state(None)
            await state.set_data({"withdrawal": snapshot})
            return (
                "✅ Способ получения денег сохранён для всех товаров: "
                + escape(snapshot["provider"].get("name") or ident)
                + ".\nКомиссии и лимиты будут проверяться заново при расчёте товара. Вывод средств не выполняется.",
                keyboard([[("Сменить способ", "providers"), ("К профилю", "profile")]]),
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
            await state.set_state(None)
            return "✅ Условия выбранного способа обновлены для всех товаров.", keyboard(
                [[("К профилю", "profile")]]
            )
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
        if action.startswith("fin:allocation:"):
            ident = action.split(":", 2)[2]
            await self.read_owned(ident)
            await state.set_state("finance:allocation")
            await state.set_data({"finance_item_id": ident})
            return (
                "Укажите расход размещения и число будущих продаж, например: 100 10. На продажу будет учтено 10 ₽. Это прогноз, исторический учёт не меняется. Для явного нулевого распределения: 0 1.",
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
        if current == "finance:allocation":
            parts = (message.text or "").split()
            if len(parts) != 2 or not parts[1].isdigit() or not 1 <= int(parts[1]) <= 100000:
                raise ValueError("Нужны сумма расходов и положительное число продаж")
            owner, ident = message.from_user.id, data["finance_item_id"]
            await self.read_owned(ident)
            per_sale = (money(parts[0]) / Decimal(parts[1])).quantize(Decimal("0.01"))
            self.store.set_setting(f"finance:{owner}:placement_per_sale:{ident}", str(per_sale))
            await state.clear()
            return await self.item(owner, ident)
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
