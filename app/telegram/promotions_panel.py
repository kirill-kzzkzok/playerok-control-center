import secrets
from dataclasses import asdict

from app.database.store import Conflict
from app.services.finance import money
from app.services.products import ValidationError, priority_price_basis
from app.services.promotion_settings import PromotionSettings
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard

EDIT = {
    "interval_seconds": "Интервал, секунд (от 600)",
    "check_seconds": "Проверка позиции, секунд (от 120)",
    "threshold": "Дневной порог позиции (от 1)",
    "cooldown_seconds": "Cooldown, секунд (от 600)",
    "night_interval_seconds": "Ночной интервал, секунд (от 600)",
    "night_threshold": "Ночной порог (от 1)",
    "night_start_hour": "Начало ночи, час МСК (0–23)",
    "night_end_hour": "Конец ночи, час МСК (0–23)",
    "per_operation": "Лимит на операцию, ₽",
    "daily": "Дневной лимит, ₽",
    "monthly": "Месячный лимит, ₽",
    "max_operations_per_item": "Максимум операций на лот (0 запрещает)",
}


class PromotionsPanel:
    def __init__(self, store, api):
        self.service, self.api = PromotionSettings(store, api), api

    async def show(self, data, state):
        data["nonce"] = secrets.token_hex(6)
        await state.set_state(None)
        await state.set_data({"promotion": data})
        nonce, values = data["nonce"], data["values"]
        text = f"🚀 <b>Продвижение · dry-run · v{data['revision']}</b>\nПолитика: {'включена для проверки' if values['enabled'] else 'выключена'}\nРежим: {'по времени' if values['mode'] == 'time' else 'по позиции'}\nВыбор: {'все подходящие кроме исключённых' if data['selection'] == 'all' else 'только выбранные'}\nНочь: {'включена' if values['night_enabled'] else 'выключена'} · {values['night_start_hour']:02d}:00–{values['night_end_hour']:02d}:00 МСК\nПлатное исполнение отсутствует. Сохранение и проверка ничего не покупают."
        rows = [
            [("Переключить политику", f"pp:{nonce}:toggle:enabled"), ("Режим", f"pp:{nonce}:mode")],
            [("Ночной режим", f"pp:{nonce}:toggle:night_enabled"), ("Выбор лотов", f"pp:{nonce}:selection")],
        ]
        for name, label in EDIT.items():
            rows.append([(f"{label}: {values[name]}", f"pp:{nonce}:edit:{name}")])
        rows.extend(
            [
                [("💾 Сохранить", f"pp:{nonce}:save")],
                [("⛔ Выключить", "promo:stop")],
                [("Выбрать товар и проверить", "items")],
                [("История расходов", "promo:expenses")],
            ]
        )
        return text, keyboard(rows)

    async def callback(self, action, owner, state):
        self.service.access.orders.owner(owner)
        parts = action.split(":")
        if action == "promo:expenses":
            rows = self.service.store.rows(
                "SELECT item_id,amount,status,created_at FROM promotion_expenses ORDER BY created_at DESC LIMIT 20"
            )
            text = "💰 <b>Сохранённые расходы продвижения</b>\n" + (
                "\n".join(
                    escaped(
                        f"{row['created_at']} · {row['amount']} ₽ · {row['status']} · {row['item_id']}", 160
                    )
                    for row in rows
                )
                if rows
                else "Записей нет. Оценки dry-run не считаются расходом."
            )
            return text, keyboard([[("Настройки", "promotions")]])
        if action in {"promotions", "promo:stop"}:
            if action == "promo:stop":
                self.service.stop(owner)
            revision, policy, selection = self.service.get()
            return await self.show(
                {"revision": revision, "values": asdict(policy), "selection": selection}, state
            )
        if len(parts) == 3 and parts[:2] == ["promo", "item"]:
            item = await self.service.access.item(owner, parts[2])
            quotes = await self.api.call(
                "get_item_priority_statuses", item_id=parts[2], item_price=priority_price_basis(item)
            )
            nonce = secrets.token_hex(6)
            data = {"id": parts[2], "nonce": nonce, "quotes": [quote["id"] for quote in quotes]}
            await state.set_state(None)
            await state.set_data({"promotion_item": data})
            revision, _, selection = self.service.get()
            rows = [
                [
                    (
                        f"{quote.get('type') or quote['id']} · {quote.get('price', '?')} ₽ · проверить",
                        f"pi:{nonce}:quote:{index}",
                    )
                ]
                for index, quote in enumerate(quotes)
            ]
            rows.extend(
                [
                    [
                        ("Включить лот", f"pi:{nonce}:target:include"),
                        ("Исключить", f"pi:{nonce}:target:exclude"),
                    ],
                    [("По общему правилу", f"pi:{nonce}:target:default")],
                    [("Настройки продвижения", "promotions")],
                ]
            )
            return (
                f"🚀 <b>Лот · проверка</b>\nВыбран: {'да' if self.service.selected(parts[2], selection) else 'нет'}\nВерсия политики: {revision}\nВыберите реальную стоимость для dry-run. Это не покупка. Политика и quote будут перечитаны.",
                keyboard(rows),
            )
        data = await state.get_data()
        if parts[0] == "pi":
            view = data.get("promotion_item", {})
            if len(parts) != 4 or parts[1] != view.get("nonce"):
                raise Conflict("Проверка товара устарела")
            if parts[2] == "target":
                await self.service.target(owner, view["id"], parts[3])
                return await self.callback("promo:item:" + view["id"], owner, state)
            if parts[2] != "quote" or not 0 <= int(parts[3]) < len(view["quotes"]):
                raise ValidationError("Стоимость недоступна")
            result = await self.service.evaluate(owner, view["id"], view["quotes"][int(parts[3])])
            return "🔎 <b>Dry-run продвижения</b>\nПо политике: " + (
                "допустимо" if result["would_allow"] else "запрещено"
            ) + "\nСтоимость: " + escaped(result["price"], 40) + " ₽\nПричины: " + escaped(
                "; ".join(result["reasons"]) or "нет", 2000
            ) + "\nПозиция: " + escaped(result["position"], 30) + "\n" + escaped(
                result["position_scope"], 200
            ) + (
                "\nПолитика локально выключена по лимиту." if result.get("policy_auto_disabled") else ""
            ) + "\nСписано: 0 ₽. Платное исполнение недоступно.", keyboard(
                [[("Лот", "promo:item:" + view["id"])]]
            )
        view = data.get("promotion", {})
        if len(parts) not in {3, 4} or parts[1] != view.get("nonce"):
            raise Conflict("Настройки продвижения устарели")
        command, values = parts[2], view["values"]
        if command == "toggle" and len(parts) == 4 and parts[3] in {"enabled", "night_enabled"}:
            values[parts[3]] = not values[parts[3]]
        elif command == "mode":
            values["mode"] = "position" if values["mode"] == "time" else "time"
        elif command == "selection":
            view["selection"] = "all" if view["selection"] == "selected" else "selected"
        elif command == "edit" and len(parts) == 4 and parts[3] in EDIT:
            view["field"] = parts[3]
            await state.update_data(promotion=view)
            await state.set_state("promotion:value")
            return EDIT[parts[3]] + ". Введите значение; затем сохраните политику.", keyboard([])
        elif command == "save":
            view["revision"] = self.service.save(owner, view["revision"], values, view["selection"])
        else:
            raise ValidationError("Действие продвижения недоступно")
        return await self.show(view, state)

    async def message(self, message, state):
        self.service.access.orders.owner(message.from_user.id)
        data = (await state.get_data())["promotion"]
        field = data.get("field")
        if field not in EDIT:
            raise ValidationError("Поле устарело")
        value = message.text or ""
        data["values"][field] = (
            str(money(value.replace(",", ".")))
            if field in {"per_operation", "daily", "monthly"}
            else int(value)
        )
        data.pop("field", None)
        return await self.show(data, state)
