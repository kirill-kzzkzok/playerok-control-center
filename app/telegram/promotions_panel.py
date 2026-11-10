import json
import secrets
from dataclasses import asdict

from app.database.store import Conflict
from app.services.finance import money
from app.services.paid_promotions import PaidPromotions
from app.services.products import ValidationError, priority_price_basis
from app.services.promotion_settings import PromotionSettings
from app.services.promotions import PromotionPolicy
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.presentation import item_label, local_time, status

EDIT = {
    "interval_seconds": "Интервал, секунд (от 600)",
    "check_seconds": "Проверка позиции, секунд (от 120)",
    "threshold": "Дневной порог позиции (от 1)",
    "cooldown_seconds": "Пауза между покупками, секунд (от 600)",
    "night_interval_seconds": "Ночной интервал, секунд (от 600)",
    "night_threshold": "Ночной порог (от 1)",
    "night_start_hour": "Начало ночи, час МСК (0–23)",
    "night_end_hour": "Конец ночи, час МСК (0–23)",
    "per_operation": "Лимит на операцию, ₽",
    "daily": "Дневной лимит, ₽",
    "monthly": "Месячный лимит, ₽",
    "max_operations_per_item": "Максимум операций на лот (0 запрещает)",
}


def buying_conditions(policy):
    def interval(seconds):
        minutes, rest = divmod(seconds, 60)
        return f"{minutes} мин" + (f" {rest} с" if rest else "")

    if policy.mode == "time":
        text = f"Поднимает товар через {interval(max(policy.interval_seconds, policy.cooldown_seconds))} после предыдущей покупки."
    else:
        text = (
            f"Поднимает, если позиция в категории ниже {policy.threshold}-й."
            f" Проверка каждые {interval(policy.check_seconds)}, покупка не чаще раза в {interval(policy.cooldown_seconds)}."
        )
    if policy.night_enabled:
        night = (
            f"интервал {interval(max(policy.night_interval_seconds, policy.cooldown_seconds))}"
            if policy.mode == "time"
            else f"поднимает ниже {policy.night_threshold}-й позиции"
        )
        text += f"\nС {policy.night_start_hour:02}:00 до {policy.night_end_hour:02}:00 МСК: {night}."
    return (
        text + f"\nЛимиты: {policy.per_operation} ₽ за раз · {policy.daily} ₽ в день · "
        f"{policy.monthly} ₽ в месяц · {policy.max_operations_per_item} покупок на товар."
        + (
            " Нулевой лимит запрещает автопокупки."
            if not all(
                money(v)
                for v in (policy.per_operation, policy.daily, policy.monthly, policy.max_operations_per_item)
            )
            else ""
        )
    )


class PromotionsPanel:
    def __init__(self, store, api):
        self.service, self.api = PromotionSettings(store, api), api

    async def settings(self, state, *, advanced=True, group="general", return_to=None):
        revision, policy, selection = self.service.get()
        return await self.show(
            {
                "revision": revision,
                "account": self.api.account.id,
                "values": asdict(policy),
                "selection": selection,
                "advanced": advanced,
                "group": group,
                "return_to": return_to,
            },
            state,
        )

    async def show(self, data, state):
        data["nonce"] = secrets.token_hex(6)
        await state.set_state(None)
        await state.set_data({"promotion": data})
        nonce, values = data["nonce"], data["values"]
        text = f"🚀 <b>Продвижение — проверка</b>\nПолитика: {'включена для проверки' if values['enabled'] else 'выключена'}\nРежим: {'по времени' if values['mode'] == 'time' else 'по позиции'}\nВыбор: {'все подходящие кроме исключённых' if data['selection'] == 'all' else 'только выбранные'}\nНочь: {'включена' if values['night_enabled'] else 'выключена'} · {values['night_start_hour']:02d}:00–{values['night_end_hour']:02d}:00 МСК\nСохранение и проверка ничего не покупают. Разрешение расходов — отдельным действием."
        if data.get("advanced"):
            text += "\n🌍 Настройки всего магазина."
            text += (
                "\nИзменения ниже остаются в форме до сохранения. Нажмите «Сохранить», чтобы применить их."
            )
        rows = [
            [
                (
                    "Автоподнятие в форме: " + ("да" if values["enabled"] else "нет"),
                    f"pp:{nonce}:toggle:enabled",
                ),
                ("Режим", f"pp:{nonce}:mode"),
            ],
            [
                (
                    "Особые условия ночью: " + ("да" if values["night_enabled"] else "нет"),
                    f"pp:{nonce}:toggle:night_enabled",
                ),
                (
                    "Участники: " + ("все товары" if data["selection"] == "all" else "выбранные"),
                    f"pp:{nonce}:selection",
                ),
            ],
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
            status = "Выключено" if not values["enabled"] else "Подготовлено; расходы ещё не разрешены"
            if values["enabled"] and (scoped or global_authority):
                try:
                    paid.permissions(automatic=True)
                    status = "Включено для подтверждённых товаров"
                except ValidationError as error:
                    status = str(error)
            from app.services.premium_review import blocking_expenses

            pending_money = blocking_expenses(self.service.store)
            if values["enabled"] and pending_money:
                status = "На паузе"
            elif status == "Включено для подтверждённых товаров":
                status = "Включено"
            text = (
                "🚀 <b>Автопродвижение</b>\n"
                + escaped(status, 160)
                + (
                    "\nУчастники: все товары"
                    if data["selection"] == "all"
                    else f"\nВыбрано товаров: {len(targets)}"
                )
                + "\n"
                + buying_conditions(PromotionPolicy(**values))
            )
            if pending_money:
                expense = pending_money[0]
                saved = json.loads(expense["payload"] or "{}")
                name = saved.get("name") or item_label(self.service.store, expense["item_id"])
                text += (
                    f"\n⛔ Покупки заблокированы: не подтверждён платёж {expense['amount']} ₽ · "
                    + escaped(name, 75)
                )
                if len(pending_money) > 1:
                    text += f" (+{len(pending_money) - 1})"
            for target in targets[:10]:
                ident = target["item_id"]
                latest = self.service.store.rows(
                    "SELECT result FROM promotion_evaluations WHERE item_id=? AND policy_revision=? ORDER BY id DESC LIMIT 1",
                    (ident, data["revision"]),
                )
                approval = self.service.store.setting("promotions:approved:" + ident, {})
                observation = json.loads(latest[0]["result"]) if latest else {}
                name = (
                    observation.get("item_name")
                    or approval.get("name")
                    or item_label(self.service.store, ident)
                )
                position = observation.get("position")
                text += "\n• " + escaped(name, 75)
                if type(position) is int:
                    text += f" · Позиция в категории: {position}"
            if len(targets) > 10:
                text += f"\nЕщё товаров: {len(targets) - 10}."
            rows = [
                [("⛔ Остановить автоподнятие", "promo:stop")]
                if values["enabled"] and (scoped or global_authority)
                else [("▶️ Включить автоподнятие", "promo:enable")],
                [("💳 Предел расходов", "promo:budget")],
                [("📦 Товары для продвижения", "promo:catalog")],
                [("Результаты и затраты", "promo:expenses")],
                [("Дополнительные параметры", f"pp:{nonce}:advanced")],
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
                    [("Ночные условия", f"pp:{nonce}:group:night")],
                    [("Бюджет и ограничения", f"pp:{nonce}:group:budget")],
                ]
            else:
                rows = [
                    [(f"{EDIT[name]}: {values[name]}", f"pp:{nonce}:edit:{name}")] for name in groups[group]
                ]
            rows.append([("💾 Сохранить", f"pp:{nonce}:save")])
        back = (
            ("⬅️ К черновику запуска", data["return_to"])
            if data.get("return_to")
            else ("⬅️ Мой магазин", "home")
        )
        if data.get("advanced"):
            back = (
                "⬅️ Назад",
                f"pp:{nonce}:group:general" if group != "general" else f"pp:{nonce}:basic",
            )
        return text, keyboard(rows, back=back)

    def item_view(self, view):
        nonce, ident = view["nonce"], view["id"]
        _revision, _policy, selection = self.service.get()
        selected = self.service.selected(ident, selection)
        rows = [
            [
                (
                    "Не участвовать" if selected else "Добавить в автоподнятие",
                    f"pi:{nonce}:target:" + ("exclude" if selected else "include"),
                )
            ],
            [("Тариф и разрешение покупок", "payp:auto:" + ident)],
            [("🗓 Условия и лимиты", "promo:settings")],
            [("Дополнительные параметры", f"pi:{nonce}:more:open")],
        ]
        text = (
            "🚀 <b>Автоподнятие товара</b>\n"
            + escaped(view.get("name") or "Товар", 100)
            + ("\nАвтоподнятие: товар выбран" if selected else "\nАвтоподнятие: товар не выбран")
            + "\nВыбор товара не разрешает расходы. Для запуска нужны подтверждённый тариф и разрешение повторных покупок."
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
                [("Использовать общее правило магазина", f"pi:{nonce}:target:default")],
            ]
        return text, keyboard(
            rows,
            back=(
                "⬅️ Назад",
                f"pi:{nonce}:more:close" if view.get("advanced") else "payp:item:" + ident,
            ),
        )

    async def callback(self, action, owner, state):
        self.service.access.orders.owner(owner)
        parts = action.split(":")
        if action == "promo:budget":
            await self.settings(state, group="budget")
            nonce = (await state.get_data())["promotion"]["nonce"]
            return await self.callback(f"pp:{nonce}:edit:daily", owner, state)
        if action == "promo:enable":
            _revision, policy, _selection = self.service.get()
            if (
                min(money(policy.per_operation), money(policy.daily), money(policy.monthly)) <= 0
                or policy.max_operations_per_item < 1
            ):
                return await self.callback("promo:budget", owner, state)
            from app.telegram.paid_promotions_panel import PaidPromotionsPanel

            result = await PaidPromotionsPanel(self.service.store, self.api).callback(
                "payp:arm", owner, state
            )
            pending = (await state.get_data())["paid_arm"]
            await state.update_data(paid_arm={**pending, "enable": not policy.enabled})
            return result
        if action == "promo:expenses":
            rows = self.service.store.rows(
                "SELECT item_id,amount,status,created_at FROM promotion_expenses ORDER BY created_at DESC LIMIT 20"
            )
            text = "💰 <b>Сохранённые расходы продвижения</b>\n" + (
                "\n".join(
                    escaped(
                        f"{local_time(row['created_at'])} · {row['amount']} ₽ · "
                        + ("Зарезервировано" if row["status"] == "RESERVED" else status(row["status"]))
                        + " · "
                        + item_label(self.service.store, row["item_id"]),
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
            return await self.settings(state, advanced=action == "promo:settings")
        if len(parts) == 3 and parts[:2] == ["promo", "item"]:
            item = await self.service.access.item(owner, parts[2])
            nonce = secrets.token_hex(6)
            data = {
                "id": parts[2],
                "nonce": nonce,
                "quotes": [],
                "name": item.get("name") or "Товар",
            }
            await state.set_state(None)
            await state.set_data({"promotion_item": data})
            return self.item_view(data)
        data = await state.get_data()
        if parts[0] == "pi":
            view = data.get("promotion_item", {})
            if len(parts) != 4 or parts[1] != view.get("nonce"):
                raise Conflict("Проверка товара устарела")
            if parts[2] == "more":
                view["advanced"] = parts[3] == "open"
                if view["advanced"]:
                    item = await self.service.access.item(owner, view["id"])
                    quotes = await self.api.call(
                        "get_item_priority_statuses",
                        item_id=view["id"],
                        item_price=priority_price_basis(item),
                    )
                    view["quotes"] = [quote["id"] for quote in quotes]
                    view["quote_previews"] = [
                        {key: quote.get(key) for key in ("type", "price", "period")} for quote in quotes
                    ]
                await state.update_data(promotion_item=view)
                return self.item_view(view)
            if parts[2] == "target":
                await self.service.target(owner, view["id"], parts[3])
                view.update(nonce=secrets.token_hex(6), advanced=False, quotes=[], quote_previews=[])
                await state.update_data(promotion_item=view)
                return self.item_view(view)
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
        if view.get("account", self.api.account.id) != self.api.account.id:
            raise Conflict("Аккаунт изменился. Откройте настройки заново")
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
            back = ("⬅️ К параметрам", f"pp:{parts[1]}:group:{view.get('group', 'general')}")
            return (
                EDIT[parts[3]]
                + f". Сейчас: {values[parts[3]]}. Введите значение; затем сохраните настройки.",
                keyboard([], back=back),
            )
        elif command == "save":
            view["revision"] = self.service.save(owner, view["revision"], values, view["selection"])
        else:
            raise ValidationError("Действие продвижения недоступно")
        return await self.show(view, state)

    async def message(self, message, state):
        self.service.access.orders.owner(message.from_user.id)
        data = (await state.get_data())["promotion"]
        field = data.get("field")
        if data.get("account", self.api.account.id) != self.api.account.id:
            raise Conflict("Аккаунт изменился. Откройте настройки заново")
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
