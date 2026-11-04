import secrets
from dataclasses import asdict
from datetime import UTC, datetime

from app.database.store import Conflict
from app.services.finance import money
from app.services.paid_promotions import PaidPromotions
from app.services.products import ValidationError, priority_price_basis
from app.services.promotion_settings import PromotionSettings
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.presentation import local_time

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
        text = f"🚀 <b>Продвижение — проверка</b>\nПолитика: {'включена для проверки' if values['enabled'] else 'выключена'}\nРежим: {'по времени' if values['mode'] == 'time' else 'по позиции'}\nВыбор: {'все подходящие кроме исключённых' if data['selection'] == 'all' else 'только выбранные'}\nНочь: {'включена' if values['night_enabled'] else 'выключена'} · {values['night_start_hour']:02d}:00–{values['night_end_hour']:02d}:00 МСК\nСохранение и проверка ничего не покупают. Разрешение расходов — отдельным действием."
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
                [("📦 Товары для продвижения", "promo:catalog")],
                [
                    ("История расходов", "aq:list:expenses:0"),
                    ("История проверки без оплаты", "aq:list:evaluations:0"),
                ],
                [
                    ("Выбранные / исключённые", "aq:list:targets:0"),
                    ("Очередь проверок", "aq:list:schedule:0"),
                ],
            ]
        )
        if not data.get("advanced"):
            paid = PaidPromotions(self.service.store, self.api)
            targets = self.service.store.rows(
                "SELECT item_id FROM promotion_targets WHERE disposition='include' ORDER BY item_id"
            )
            scoped = any(paid.has_authority(row["item_id"], data["revision"]) for row in targets)
            permission = self.service.store.setting("promotions:paid_authority", {})
            global_authority = (
                permission.get("revision") == data["revision"]
                and permission.get("account_id") == self.api.account.id
            )
            status = "Остановлено" if not values["enabled"] else "Проверка без оплаты: расходы не разрешены"
            if values["enabled"] and (scoped or global_authority):
                try:
                    paid.permissions(automatic=True)
                    status = "Платное расписание разрешено; тарифы проверяются перед выполнением"
                except ValidationError as error:
                    status = str(error)
            due = self.service.store.rows(
                "SELECT MIN(due_at) AS due FROM promotion_schedule WHERE policy_revision=?",
                (data["revision"],),
            )[0]["due"]
            next_check = (
                local_time(datetime.fromtimestamp(due, UTC).isoformat())
                if due
                else "при следующем цикле фоновой задачи"
            )
            text = (
                "🚀 <b>Автопродвижение</b>\n"
                + escaped(status, 200)
                + f"\nВыбрано товаров: {len(targets)}"
                + ("; действует общее правило магазина" if data["selection"] == "all" else "")
                + f"\nЛимиты: {values['per_operation']} ₽ / операция · {values['daily']} ₽ / сутки · {values['monthly']} ₽ / месяц"
                + "\nСледующая проверка: "
                + next_check
            )
            health = self.service.store.setting("promotions:health", {})
            if (
                values["enabled"]
                and health.get("revision") == data["revision"]
                and health.get("status") == "DEGRADED"
            ):
                text += f"\n⚠️ Ошибки проверки товаров: {health.get('failed_items', 0)}"
                if health.get("discovery_error"):
                    text += "\nНе удалось обновить список товаров."
                if health.get("last_success"):
                    text += "\nПоследняя успешная проверка: " + local_time(health["last_success"])
            rows = [
                [("📦 Товары для продвижения", "promo:catalog")],
                [("💳 Разрешить платное расписание", "payp:arm")],
                [("🗓 Расписание и лимиты", f"pp:{nonce}:advanced")],
                [("История продвижения", "tools:promotion")],
                [("⛔ Остановить продвижение", "promo:stop")],
            ]
        else:
            group = data.get("group", "general")
            groups = {
                "general": [],
                "time": ["interval_seconds", "check_seconds", "threshold", "cooldown_seconds"],
                "night": ["night_interval_seconds", "night_threshold", "night_start_hour", "night_end_hour"],
                "budget": ["per_operation", "daily", "monthly", "max_operations_per_item"],
            }
            if group not in groups:
                raise ValidationError("Экран настройки устарел")
            if group == "general":
                rows = rows[:2] + [
                    [("Интервалы и позиция", f"pp:{nonce}:group:time")],
                    [("Ночное расписание", f"pp:{nonce}:group:night")],
                    [("Бюджет и ограничения", f"pp:{nonce}:group:budget")],
                ]
            else:
                rows = [
                    [(f"{EDIT[name]}: {values[name]}", f"pp:{nonce}:edit:{name}")] for name in groups[group]
                ]
                rows.append([("⬅️ К параметрам", f"pp:{nonce}:group:general")])
            rows += [[("💾 Сохранить", f"pp:{nonce}:save")], [("⬅️ Основной экран", f"pp:{nonce}:basic")]]
        return text, keyboard(rows, back=("⬅️ Мой магазин", "home"))

    def item_view(self, view):
        nonce, ident = view["nonce"], view["id"]
        _revision, _policy, selection = self.service.get()
        selected = self.service.selected(ident, selection)
        rows = [
            [
                (
                    "Не участвовать" if selected else "Участвовать в расписании",
                    f"pi:{nonce}:target:" + ("exclude" if selected else "include"),
                )
            ],
            [("🗓 Расписание и лимиты", "promo:settings")],
            [("Дополнительные параметры", f"pi:{nonce}:more:open")],
        ]
        text = (
            "🚀 <b>Автоподнятие товара</b>\n"
            + escaped(view.get("name") or "Товар", 100)
            + ("\nУчастие в расписании: да" if selected else "\nУчастие в расписании: нет")
            + "\nВыбор товара не разрешает расходы. Для запуска нужны подтверждённый тариф и разрешение расписания."
        )
        if view.get("advanced"):
            text = "⚙️ <b>Дополнительные параметры</b>\nПроверка тарифов ничего не покупает. Наследование отменяет только индивидуальный выбор товара."
            rows = [
                [
                    (
                        f"Проверить без оплаты · {q.get('type', '?')} · {q.get('price', '?')} ₽",
                        f"pi:{nonce}:quote:{i}",
                    )
                ]
                for i, q in enumerate(view.get("quote_previews", []))
            ]
            rows += [
                [("Наследовать настройки магазина", f"pi:{nonce}:target:default")],
                [("К расписанию товара", f"pi:{nonce}:more:close")],
            ]
        return text, keyboard(rows, back=("К продвижению товара", "payp:item:" + ident))

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
                        f"{local_time(row['created_at'])} · {row['amount']} ₽ · {row['status']} · {row['item_id']}",
                        160,
                    )
                    for row in rows
                )
                if rows
                else "Записей нет. Оценки проверки без оплаты не считаются расходом."
            )
            return text, keyboard([[("Настройки", "promotions")]])
        if action == "promo:stop" or action.startswith("promo:stop_confirm:"):
            from app.telegram.stop_confirmation import StopConfirmation

            confirmation = StopConfirmation(self.service.store, self.api)
            if action == "promo:stop":
                return await confirmation.request("promo", owner, state)
            await confirmation.confirm("promo", owner, state, action.split(":", 2)[2], self.service.stop)
            action = "promotions"
        if action in {"promotions", "promo:settings"}:
            revision, policy, selection = self.service.get()
            return await self.show(
                {
                    "revision": revision,
                    "values": asdict(policy),
                    "selection": selection,
                    "advanced": action == "promo:settings",
                },
                state,
            )
        if len(parts) == 3 and parts[:2] == ["promo", "item"]:
            item = await self.service.access.item(owner, parts[2])
            quotes = await self.api.call(
                "get_item_priority_statuses", item_id=parts[2], item_price=priority_price_basis(item)
            )
            nonce = secrets.token_hex(6)
            data = {
                "id": parts[2],
                "nonce": nonce,
                "quotes": [quote["id"] for quote in quotes],
                "quote_previews": [
                    {key: quote.get(key) for key in ("type", "price", "period")} for quote in quotes
                ],
            }
            await state.set_state(None)
            await state.set_data({"promotion_item": data})
            data["name"] = item.get("name") or "Товар"
            await state.set_data({"promotion_item": data})
            return self.item_view(data)
        data = await state.get_data()
        if parts[0] == "pi":
            view = data.get("promotion_item", {})
            if len(parts) != 4 or parts[1] != view.get("nonce"):
                raise Conflict("Проверка товара устарела")
            if parts[2] == "more":
                view["advanced"] = parts[3] == "open"
                await state.update_data(promotion_item=view)
                return self.item_view(view)
            if parts[2] == "target":
                await self.service.target(owner, view["id"], parts[3])
                return await self.callback("promo:item:" + view["id"], owner, state)
            if parts[2] != "quote" or not 0 <= int(parts[3]) < len(view["quotes"]):
                raise ValidationError("Стоимость недоступна")
            result = await self.service.evaluate(owner, view["id"], view["quotes"][int(parts[3])])
            return "🔎 <b>Проверка продвижения</b>\nПо политике: " + (
                "допустимо" if result["would_allow"] else "запрещено"
            ) + "\nСтоимость: " + escaped(result["price"], 40) + " ₽\nПричины: " + escaped(
                "; ".join(result["reasons"]) or "нет", 2000
            ) + "\nПозиция: " + escaped(result["position"], 30) + "\n" + escaped(
                result["position_scope"], 200
            ) + (
                "\nПолитика локально выключена по лимиту." if result.get("policy_auto_disabled") else ""
            ) + "\nСписано: 0 ₽. Платное действие требует отдельного подтверждения.", keyboard(
                [[("Лот", "promo:item:" + view["id"])]]
            )
        view = data.get("promotion", {})
        if len(parts) not in {3, 4} or parts[1] != view.get("nonce"):
            raise Conflict("Настройки продвижения устарели")
        command, values = parts[2], view["values"]
        if command in {"advanced", "basic"}:
            view["advanced"] = command == "advanced"
        elif command == "group" and len(parts) == 4 and parts[3] in {"general", "time", "night", "budget"}:
            view["group"] = parts[3]
        elif command == "toggle" and len(parts) == 4 and parts[3] in {"enabled", "night_enabled"}:
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
