"""Durable setup within the product wizard, before the final publication confirmation."""

import secrets
from dataclasses import asdict

from app.database.store import Conflict
from app.gifts.core import gift_name
from app.services.automation import message_text
from app.services.inventory import stock_values
from app.services.listing_launch import ListingLaunch, launch_prefix
from app.services.products import ValidationError, priority_price_basis
from app.services.promotions import PromotionPolicy
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.storage import replace_context

MODES = {
    "manual": "Ручная выдача",
    "text": "Постоянный текст",
    "stock": "Уникальные данные со склада",
    "gifts": "Telegram Gifts",
}
FIELDS = {
    "data": "Введите текст выдачи или запас, по одной единице на строку. Содержимое не будет показано в превью.",
    "greeting": "Приветствие перед текстовой/складской выдачей. «-» — без приветствия.",
    "after": "Сообщение после подтверждённой выдачи. «-» — стандартное сообщение или без дополнительного текста.",
    "limits": "Лимиты Stars: за заказ и за сутки через пробел. Они не могут превышать ограничения подключения. Дневной предел учитывает общие расходы магазина.",
    "budget": "Общие лимиты продвижения Playerok: за операцию, сутки, месяц, количество операций на товар. Например: 50 100 1000 20. Сохранение не списывает деньги; применятся при итоговом запуске.",
    "schedule": "Расписание автоподнятия: интервал и пауза между покупками, в секундах (минимум 600), например: 3600 600.",
}


class LaunchPanel:
    def __init__(self, store, api, gifts):
        self.service = ListingLaunch(store, api, gifts)
        self.store, self.api = store, api

    async def show(self, owner, state, ident):
        draft = self.service.load(owner, ident)
        value = draft["payload"]
        item = await self.service.automation.item(owner, value["launch_target"])
        if draft["state"] != "DRAFT":
            ops = self.store.rows(
                "SELECT id FROM outbox WHERE operation_key LIKE ? ORDER BY created_at DESC LIMIT 1",
                (launch_prefix(ident),),
            )
            return (
                "⏳ Запуск уже подтверждён. Повторная публикация заблокирована до проверки результата.",
                keyboard(
                    [[("Проверить результат", "result:show:" + ops[0]["id"])]]
                    if ops
                    else [[("Обновить состояние", "launch:open:" + ident)]]
                ),
            )
        nonce = secrets.token_hex(6)
        await replace_context(
            state, None, {"launch": {"id": ident, "revision": draft["revision"], "nonce": nonce}}
        )
        p = f"ln:{nonce}:"
        ready = value.get("ready")
        needs_choice = self.service.needs_delivery_choice(owner, value)
        if needs_choice:
            ready = None
        text = (
            "🚦 <b>Подготовка к публикации</b>\n"
            + escaped(item.get("name") or "Товар", 200)
            + f" · {escaped(item.get('price'), 30)} ₽\n"
            + "Размещение: "
            + ("бесплатно" if value["publication"] == "DEFAULT" else "PREMIUM")
            + "\nВыдача: "
            + MODES[value["mode"]]
            + "\nАвтовыдача: "
            + (
                "не применяется"
                if value["mode"] == "manual"
                else "готова к предварительному включению"
                if ready
                else "требует проверки"
            )
            + "\nЗавершение заказа: "
            + (
                "автоматически, только если Playerok не требует отчёт"
                if value["auto_complete"]
                else "вручную"
            )
        )
        if value["mode"] in {"text", "stock"}:
            text += "\nДанные: " + ("подготовлены, содержимое скрыто" if value["data"] else "не заданы")
            if value["mode"] == "stock" and value["data"]:
                values, empty, duplicates = stock_values(value["data"])
                text += f" · {len(values)} ед.; пустых строк {empty}, повторов {duplicates}"
        text += "\nПриветствие: " + ("задано" if value["greeting"] else "нет")
        text += "\nПосле выдачи: " + (
            "задано" if value["after"] else "стандартное / без дополнительного текста"
        )
        if value["mode"] == "gifts":
            text += f"\nЛимиты Stars: {value['order_limit']} за заказ / {value['day_limit']} в сутки"
            text += "\nВозврат: " + (
                "по подтверждённым правилам, до первой оплаты" if value["refund"] else "ручное решение"
            )
            text += "\nСостав: " + escaped(
                ", ".join(gift_name(code.strip()) for code in value["gift_code"].split(","))
                if value.get("gift_code")
                else (item.get("description") or "").splitlines()[-1]
                if item.get("description")
                else "не указан",
                200,
            )
        text += "\nАвтоподнятие: " + (
            "включить по указанным общим настройкам" if value["promotion"] else "выключено"
        )
        if value["publication"] == "PREMIUM":
            policy = value["promotion_policy"]
            text += "\nОплата размещения: баланс или заморозка дохода, если её разрешает Playerok."
            text += f"\nРасходы Playerok: до {policy['per_operation']} ₽ за раз / {policy['daily']} ₽ в сутки / {policy['monthly']} ₽ в месяц"
            text += (
                f"\nРасписание: каждые {policy['interval_seconds']} с; пауза {policy['cooldown_seconds']} с"
            )
        if ready:
            text += f"\nПроверено размещение: {ready['quote']['price']} ₽"
            if ready.get("bundle"):
                text += "\n" + ", ".join(
                    f"{escaped(gift_name(g['code']), 60)} — {g['stars']} ⭐" for g in ready["bundle"]
                )
                text += f"\nВсего: {sum(g['stars'] for g in ready['bundle'])} ⭐; аккаунт и поле получения проверены"
            if ready.get("units") is not None:
                text += f"\nБудет доступно: {ready['units']} единиц"
        rows = [
            [("⚡ Способ выдачи", p + "modes")],
            [("⭐ Тип публикации и тариф", p + "quotes")],
            [("✅ Проверить готовность", p + "check")],
        ]
        if self.store.rows("SELECT 1 FROM logical_listings WHERE item_id=?", (item["id"],)):
            rows.insert(
                1, [("Наследовать настройки: " + ("да" if value.get("inherit") else "нет"), p + "inherit")]
            )
        if value["mode"] in {"text", "stock"}:
            rows.insert(1, [("🔐 Данные выдачи", p + "edit:data"), ("Приветствие", p + "edit:greeting")])
        if value["mode"] != "manual":
            rows.insert(1, [("Сообщение после выдачи", p + "edit:after"), ("Автозавершение", p + "complete")])
        if value["mode"] == "gifts":
            rows.insert(1, [("🎁 Выбрать подарки", p + "catalog"), ("Лимиты Stars", p + "edit:limits")])
            rows.insert(2, [("Автовозврат: переключить", p + "refund")])
        if value["publication"] == "PREMIUM":
            rows.insert(1, [("Лимиты Playerok", p + "edit:budget"), ("Расписание", p + "edit:schedule")])
            rows.insert(2, [("Платное автоподнятие: переключить", p + "promotion")])
        if ready:
            rows.append([("🚀 Опубликовать и запустить", p + "commit")])
        if needs_choice:
            text += "\n⚠️ Ранее выбрана автовыдача. Выберите способ выдачи заново; прежний ручной режим не подтверждён."
        rows.append([("💾 Сохранить и выйти", "home")])
        return text, keyboard(rows, back=("⬅️ К товару", "item:" + value["launch_target"]))

    async def callback(self, action, owner, state):
        if action.startswith("launch:"):
            _, command, ident = action.split(":", 2)
            if command == "from":
                source = self.store.get_draft(ident, owner)
                binding = self.store.rows("SELECT remote_id FROM bindings WHERE draft_id=?", (ident,))
                if not binding:
                    return (
                        "⏳ Серверный черновик ещё не подтверждён. Проверьте результат создания, затем продолжите настройку здесь.",
                        keyboard([[("Обновить", action)]]),
                    )
                ident = await self.service.open(owner, binding[0]["remote_id"], source["payload"])
            elif command == "item":
                ident = await self.service.open(owner, ident)
            elif command != "open":
                raise Conflict("Экран запуска устарел")
            return await self.show(owner, state, ident)
        if action == "ln:policy:apply":
            data = await state.get_data()
            proposal = data.get("launch_policy_proposal")
            if not proposal:
                raise Conflict("Предложение настройки устарело. Введите значения заново")
            await state.update_data(launch_policy_proposal=None)
            from app.services.promotion_settings import PromotionSettings

            settings = PromotionSettings(self.store, self.api)
            revision, _current, selection = settings.get()
            if revision != proposal["revision"]:
                # Never overwrite a newer concurrent policy change.
                raise Conflict(
                    "Общие настройки продвижения изменились в другом экране. "
                    "Откройте лимиты заново и повторите ввод"
                )
            settings.save(owner, proposal["revision"], proposal["policy"], selection)
            _, saved, _ = settings.get()
            draft = self.service.load(owner, proposal["draft_id"])
            if draft["state"] == "DRAFT":
                payload = draft["payload"]
                payload["promotion_policy"] = asdict(saved)
                payload.pop("ready", None)
                self.store.update_draft(draft["id"], owner, draft["revision"], payload)
            return (
                (
                    "✅ Общие настройки продвижения сохранены. Черновик запуска синхронизирован.\n"
                    "Разрешённые автоподнятия других лотов потребуют повторного подтверждения."
                ),
                keyboard([[("Продолжить настройку", "launch:open:" + proposal["draft_id"])]]),
            )
        parts = action.split(":")
        view = (await state.get_data()).get("launch", {})
        if len(parts) < 3 or view.get("nonce") != parts[1]:
            raise Conflict("Кнопка настройки устарела. Откройте черновик запуска заново")
        draft = self.service.load(owner, view["id"])
        if draft["revision"] != view["revision"] or draft["state"] != "DRAFT":
            raise Conflict("Настройки уже изменены или запуск подтверждён")
        value, command = draft["payload"], parts[2]
        p = f"ln:{parts[1]}:"
        if command == "modes":
            return "⚡ Выберите выдачу до публикации.", keyboard(
                [[(label, p + "mode:" + mode)] for mode, label in MODES.items()],
                back=("Назад", "launch:open:" + draft["id"]),
            )
        if command == "mode":
            if parts[3] not in MODES:
                raise ValidationError("Неизвестный способ выдачи")
            value["mode"] = parts[3]
            value["manual_delivery_confirmed"] = parts[3] == "manual"
        elif command in {"complete", "refund", "promotion", "inherit"}:
            key = "auto_complete" if command == "complete" else command
            value[key] = not value.get(key, False)
        elif command == "edit":
            field = parts[3]
            if field not in FIELDS:
                raise ValidationError("Поле недоступно")
            await state.set_state("launch:" + field)
            return FIELDS[field], keyboard([], back=("Отмена", "launch:open:" + draft["id"]))
        elif command == "quotes":
            item = await self.service.automation.item(owner, value["launch_target"])
            quotes = await self.service.products.priority_preview(item["id"], priority_price_basis(item))
            choices = [
                q for q in quotes if q.get("type") in {"DEFAULT", "PREMIUM"} and q.get("price") is not None
            ]
            await state.update_data(launch_quotes=choices)
            return (
                "⭐ Выберите актуальный тариф. Расход подтвердится только итоговой кнопкой запуска.",
                keyboard(
                    [
                        [
                            (
                                f"{q['type']} · {q['price']} ₽ · {q.get('period', '?')} дн.",
                                p + "quote:" + str(i),
                            )
                        ]
                        for i, q in enumerate(choices)
                    ],
                    back=("Назад", "launch:open:" + draft["id"]),
                ),
            )
        elif command == "quote":
            choices = (await state.get_data()).get("launch_quotes", [])
            if not parts[3].isdigit() or not 0 <= int(parts[3]) < len(choices):
                raise Conflict("Тариф устарел")
            selected = choices[int(parts[3])]
            value.update(publication=selected["type"], quote_id=selected["id"])
            value["manual_premium"] = selected["type"] == "PREMIUM"
            if selected["type"] == "DEFAULT":
                value["promotion"] = False
        elif command == "catalog":
            await self.service.gifts.refresh()
            entries = self.store.rows("SELECT code,stars FROM gift_catalog WHERE available=1 ORDER BY code")
            await state.update_data(launch_gifts=[])
            return (
                "🎁 Выберите подарки по порядку. Повторное нажатие добавляет ещё один такой подарок. После выбора сохраните состав в серверный черновик.",
                keyboard(
                    [
                        [(f"{gift_name(g['code'])} · {g['stars']} ⭐", p + "gift:" + g["code"])]
                        for g in entries
                    ],
                    back=("Отмена", "launch:open:" + draft["id"]),
                ),
            )
        elif command == "gift":
            chosen = (await state.get_data()).get("launch_gifts", [])
            if (
                not self.store.rows("SELECT 1 FROM gift_catalog WHERE code=? AND available=1", (parts[3],))
                or len(chosen) >= self.service.gifts.settings.max_gifts
            ):
                raise ValidationError("Подарок недоступен или превышено допустимое количество")
            chosen.append(parts[3])
            await state.update_data(launch_gifts=chosen)
            entries = self.store.rows("SELECT code,stars FROM gift_catalog WHERE available=1 ORDER BY code")
            return "🎁 Выбрано: " + ", ".join(gift_name(code) for code in chosen), keyboard(
                [[(f"{gift_name(g['code'])} · {g['stars']} ⭐", p + "gift:" + g["code"])] for g in entries]
                + [[("Сохранить состав в черновик Playerok", p + "savegifts")]],
                back=("Отмена", "launch:open:" + draft["id"]),
            )
        elif command == "savegifts":
            chosen = (await state.get_data()).get("launch_gifts", [])
            if not chosen:
                raise ValidationError("Выберите хотя бы один подарок")
            item = await self.service.automation.item(owner, value["launch_target"])
            if item.get("status") != "DRAFT":
                raise Conflict("Изменение состава разрешено только неопубликованному черновику")
            if self.store.rows("SELECT 1 FROM logical_listings WHERE item_id=?", (item["id"],)):
                value["gift_code"] = ", ".join(chosen)
                value.pop("ready", None)
                self.store.update_draft(draft["id"], owner, draft["revision"], value)
                return await self.show(owner, state, draft["id"])
            lines = [
                line
                for line in (item.get("description") or "").splitlines()
                if not line.strip().casefold().startswith("gift:")
            ]
            description = "\n".join(lines).rstrip() + "\ngift: " + ", ".join(chosen)
            op = self.store.enqueue(
                f"launch-gifts:{draft['id']}:{draft['revision']}",
                "edit",
                item["id"],
                {"description": description},
                actor=owner,
            )
            value.pop("ready", None)
            self.store.update_draft(draft["id"], owner, draft["revision"], value)
            await state.clear()
            return (
                "⏳ Состав сохраняется в неопубликованный черновик. После подтверждения продолжите настройку.",
                keyboard(
                    [
                        [("Результат изменения", "result:show:" + op)],
                        [("Продолжить настройку запуска", "launch:open:" + draft["id"])],
                    ]
                ),
            )
        elif command == "check":
            value["ready"] = await self.service.check(owner, value)
            self.store.update_draft(draft["id"], owner, draft["revision"], value)
            return await self.show(owner, state, draft["id"])
        elif command == "commit":
            op = await self.service.queue(owner, draft["id"], draft["revision"])
            await state.clear()
            return (
                "⏳ Выдача подготовлена до публикации. Проверяем серверный результат; товар пока не объявлен активным.",
                keyboard([[("Результат публикации", "result:show:" + op)]]),
            )
        else:
            raise Conflict("Кнопка запуска устарела")
        value.pop("ready", None)
        self.store.update_draft(draft["id"], owner, draft["revision"], value)
        return await self.show(owner, state, draft["id"])

    async def message(self, message, state):
        field = (await state.get_state()).removeprefix("launch:")
        view = (await state.get_data()).get("launch", {})
        draft = self.service.load(message.from_user.id, view.get("id"))
        if draft["revision"] != view.get("revision") or draft["state"] != "DRAFT":
            raise Conflict("Настройки изменились. Откройте черновик заново")
        value = draft["payload"]
        text = message.text or ""
        if field in {"greeting", "after"}:
            value[field] = message_text("" if text.strip() == "-" else text)
        elif field == "data":
            if value["mode"] == "stock":
                _, empty, duplicates = stock_values(text)
                if empty or duplicates:
                    raise ValidationError("Удалите пустые строки и повторяющиеся значения")
                value["data"] = text
            else:
                value["data"] = message_text(text, required=True)
        elif field == "limits":
            parts = text.split()
            if len(parts) != 2 or not all(p.isdigit() for p in parts):
                raise ValidationError("Введите два целых положительных лимита Stars")
            value["order_limit"], value["day_limit"] = map(int, parts)
        elif field in {"budget", "schedule"}:
            parts = text.split()
            if len(parts) != (4 if field == "budget" else 2):
                raise ValidationError("Неверное количество параметров")
            policy = dict(value["promotion_policy"])
            if field == "budget":
                policy.update(
                    per_operation=parts[0],
                    daily=parts[1],
                    monthly=parts[2],
                    max_operations_per_item=int(parts[3]),
                )
            else:
                policy.update(mode="time", interval_seconds=int(parts[0]), cooldown_seconds=int(parts[1]))
            PromotionPolicy(**policy)
            # The budget and schedule are GLOBAL promotion settings shared by all
            # lots. Stage the proposal in the FSM and require an explicit owner
            # confirmation BEFORE the global policy is written; the draft payload
            # stays untouched when the owner declines (no lost-update window).
            from app.services.promotion_settings import PromotionSettings

            settings = PromotionSettings(self.store, self.api)
            revision, current, _selection = settings.get()
            await state.set_state(None)
            await state.update_data(
                launch_policy_proposal={
                    "draft_id": draft["id"],
                    "revision": revision,
                    "policy": policy,
                    "field": field,
                    "current": asdict(current),
                }
            )
            current_dict = asdict(current)
            diff_keys = [k for k in policy if policy[k] != current_dict.get(k)]
            scope = (
                "лимиты расходов: за операцию / сутки / месяц / операций на лот"
                if field == "budget"
                else "расписание автоподнятия"
            )
            return (
                "🌍 <b>Изменение ОБЩИХ настроек продвижения</b>\n"
                "Применяются ко всем товарам магазина. Меняются: "
                + escaped(", ".join(diff_keys) or "нет изменений", 300)
                + "\nОбласть: "
                + scope
                + ".\n\n⚠️ Действующие платные разрешения других лотов потребуют "
                "повторного подтверждения после сохранения.\n\n"
                "Данные выдачи и черновик запуска не затрагиваются.",
                keyboard(
                    [
                        [("✅ Сохранить общие настройки", "ln:policy:apply")],
                        [("Отмена", "launch:open:" + draft["id"])],
                    ]
                ),
            )
        else:
            raise Conflict("Поле настройки устарело")
        value.pop("ready", None)
        self.store.update_draft(draft["id"], message.from_user.id, draft["revision"], value)
        return await self.show(message.from_user.id, state, draft["id"])
