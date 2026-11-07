"""Durable setup within the product wizard, before the final publication confirmation."""

import json
import secrets
from dataclasses import asdict

from app.database.store import Conflict
from app.gifts.core import gift_codes, gift_name
from app.services.automation import message_text
from app.services.inventory import stock_values
from app.services.listing_launch import ListingLaunch, launch_prefix
from app.services.products import ValidationError, priority_price_basis
from app.services.promotion_settings import PromotionSettings
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.storage import replace_context

MODES = {
    "manual": "Ручная выдача",
    "text": "Постоянный текст",
    "stock": "Уникальные данные со склада",
    "gifts": "Telegram Gifts",
    "playerok": "📦 Выдаёт Playerok",
}
FIELDS = {
    "data": "Введите текст выдачи или запас, по одной единице на строку. Содержимое не будет показано в превью.",
    "greeting": "Приветствие перед текстовой/складской выдачей. «-» — без приветствия.",
    "after": "Сообщение после подтверждённой выдачи. «-» — стандартное сообщение или без дополнительного текста.",
    "limits": "Лимиты Stars: за заказ и за сутки через пробел. Они не могут превышать ограничения подключения. Дневной предел учитывает общие расходы магазина.",
}


class LaunchPanel:
    def __init__(self, store, api, gifts):
        self.service = ListingLaunch(store, api, gifts)
        self.store, self.api = store, api

    async def show(self, owner, state, ident, *, advanced=False):
        draft = self.service.load(owner, ident)
        value = draft["payload"]
        from app.services.product_readiness import ProductReadiness

        item = await ProductReadiness(self.store, self.api).ui_item(owner, value["launch_target"])
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
        preparation_error = None
        connection = getattr(self.api, "connection", None)
        verifying = (
            connection is not None
            and getattr(connection, "desired_writes", False)
            and not getattr(self.api.account, "connection_verified", False)
        )
        if verifying:
            preparation_error = (
                "Бот перепроверяет Playerok. Публикация станет доступна после подтверждения аккаунта."
            )
        elif not self.api.account.writes_enabled:
            preparation_error = "Записи Playerok выключены. Публикация недоступна."
        elif self.service.wants_automation(value) and not self.api.account.automation_enabled:
            preparation_error = "Автоматизация выключена в подключении. Запуск выдачи недоступен."
        elif value["mode"] == "gifts" and self.store.setting("gifts:stopped", True):
            preparation_error = (
                "Подарочная выдача остановлена владельцем. Возобновите её в настройках Telegram-подарков."
            )
        if preparation_error and not verifying and value.pop("ready", None):
            self.store.update_draft(ident, owner, draft["revision"], value)
            draft = self.service.load(owner, ident)
        nonce = secrets.token_hex(6)
        await replace_context(
            state, None, {"launch": {"id": ident, "revision": draft["revision"], "nonce": nonce}}
        )
        p = f"ln:{nonce}:"
        ready = value.get("ready")
        needs_choice = self.service.needs_delivery_choice(owner, value)
        if needs_choice:
            ready = None
        can_launch = (
            not preparation_error and not needs_choice and (bool(ready) or value["publication"] == "DEFAULT")
        )
        from app.services.inventory import Inventory
        from app.services.logical_products import LogicalProducts

        product = LogicalProducts(self.store, self.api).listing(item["id"])
        stock = (
            Inventory(self.store, self.api).counts(item["id"])["AVAILABLE"]
            if value["mode"] == "stock"
            else None
        )
        configured = (
            product
            and product["state"] == "VERIFIED"
            and self.service.source_configuration(owner, item["id"]).get("delivery") == value["mode"]
            and value["mode"] != "manual"
            and (stock is None or stock > 0)
        )
        if configured:
            placement = "Бесплатно" if value["publication"] == "DEFAULT" else "PREMIUM"
            if ready and value["publication"] == "PREMIUM":
                placement += f" · {ready['quote']['price']} ₽"
            text = (
                "🚦 <b>Запуск продаж</b>\n"
                + escaped(item.get("name") or "Товар", 160)
                + "\nВыдача: "
                + MODES[value["mode"]]
                + (f" · в наличии {stock} шт." if stock is not None else "")
                + ("\nВыдача подготовлена." if ready else "\nНастройки выдачи сохранены.")
                + " До публикации заказы не обрабатываются."
            )
            if preparation_error:
                text += "\n⚠️ " + preparation_error
            if advanced and value["publication"] == "PREMIUM":
                return text, keyboard(
                    [
                        [("Автоподнятие: " + ("вкл" if value["promotion"] else "выкл"), p + "promotion")],
                        [("Лимиты продвижения магазина", p + "shop")],
                    ],
                    back=("⬅️ К запуску", p + "basic"),
                )
            rows = [[("⭐ Размещение: " + placement, p + "quotes")]]
            if value["publication"] == "PREMIUM":
                rows.append([("Настройки автоподнятия", p + "advanced")])
            if can_launch:
                rows.append([("🚀 Опубликовать и запустить", p + "commit")])
            return text, keyboard(rows, back=("⬅️ К товару", "item:" + value["launch_target"]))
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
                "не требуется — товар выдаёт площадка"
                if value["mode"] == "playerok"
                else "не применяется"
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
        if value["mode"] in {"text", "stock"} and value["greeting"]:
            text += "\nПриветствие: " + ("задано" if value["greeting"] else "выключено")
        if value["mode"] in {"text", "stock", "gifts"}:
            text += "\nПосле выдачи: " + (
                "своё сообщение"
                if value["after"]
                else "стандартное уведомление"
                if value["mode"] == "gifts"
                else "выключено"
            )
        if value["mode"] == "gifts":
            day = value["day_limit"]
            text += f"\nЛимиты Stars: {value['order_limit']} за заказ"
            if day is not None:
                text += f" / {day} в сутки при включённом суточном потолке"
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
        rows = []
        if self.store.rows("SELECT 1 FROM logical_listings WHERE item_id=?", (item["id"],)):
            text += "\nПосле перевыставления: " + (
                "настройки наследуют проверенные перевыставления с тем же кодом. Отдельные денежные разрешения и ограничения сохраняются."
                if value.get("inherit")
                else "каждое новое объявление проверяется отдельно; автоматическое наследование выключено."
            )
            rows.insert(
                1,
                [
                    (
                        "Сохранять выдачу после перевыставления: "
                        + ("да" if value.get("inherit") else "нет"),
                        p + "inherit",
                    )
                ],
            )
        if value["mode"] in {"text", "stock"}:
            rows.insert(1, [("🔐 Данные выдачи", p + "edit:data")])
        if value["mode"] == "playerok":
            text += "\nСообщение после оплаты: " + (
                "включено" if value.get("platform_reply") else "выключено"
            )
            text += "\nБот не отправляет товар повторно. Действия по заказу разрешаются только после подтверждения выдачи Playerok."
            rows.insert(
                1,
                [
                    (
                        "Сообщение после оплаты: " + ("вкл" if value.get("platform_reply") else "выкл"),
                        p + "platform_reply",
                    )
                ],
            )
            if value.get("platform_reply"):
                rows.insert(2, [("Изменить сообщение после оплаты", p + "edit:greeting")])
            rows.insert(2, [("Автоматическая отметка выполнения", p + "complete")])
        elif value["mode"] != "manual":
            rows.insert(
                1,
                [
                    ("Сообщение после выдачи", p + "edit:after"),
                    (
                        "Завершение заказа: " + ("авто" if value["auto_complete"] else "вручную"),
                        p + "complete",
                    ),
                ],
            )
        if value["mode"] == "gifts":
            rows.insert(1, [("🎁 Выбрать подарки", p + "catalog"), ("Лимиты Stars", p + "edit:limits")])
            rows.insert(2, [("Автовозврат: переключить", p + "refund")])
        if value["publication"] == "PREMIUM":
            rows.insert(
                1,
                [("🌍 Продвижение всего магазина", p + "shop")],
            )
            rows.insert(2, [("Платное автоподнятие: переключить", p + "promotion")])
        if needs_choice:
            text += "\n⚠️ Ранее выбрана автовыдача. Выберите способ выдачи заново; прежний ручной режим не подтверждён."
        if preparation_error:
            text += "\n⚠️ " + preparation_error
        if not advanced:
            rows = [[("📦 Способ выдачи", p + "modes"), ("⭐ Размещение", p + "quotes")]]
            if value["mode"] in {"text", "stock"}:
                rows.append([("📦 Данные товара", p + "edit:data")])
            elif value["mode"] == "gifts":
                rows.append([("🎁 Выбрать подарок", p + "catalog")])
            rows.append([("Дополнительные параметры", p + "advanced")])
            if can_launch:
                rows.append([("🚀 Опубликовать и запустить", p + "commit")])
        back = ("⬅️ К запуску", p + "basic") if advanced else ("⬅️ К товару", "item:" + value["launch_target"])
        return text, keyboard(rows, back=back)

    async def shop_settings(self, state, ident, *, group="general"):
        from app.telegram.promotions_panel import PromotionsPanel

        return await PromotionsPanel(self.store, self.api).settings(
            state, group=group, return_to="launch:policy:" + ident
        )

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
            elif command == "prepare":
                ident = await self.service.open(owner, ident)
                draft = self.service.load(owner, ident)
                value = draft["payload"]
                if draft["state"] != "DRAFT" or not self.service.wants_automation(value):
                    raise Conflict("Сначала настройте выдачу для черновика")
                if value["publication"] != "DEFAULT":
                    return await self.show(owner, state, ident)
                from app.services.sync_health import orders_sync_ready

                if not (
                    self.api.account.writes_enabled
                    and self.api.account.automation_enabled
                    and orders_sync_ready(self.store)
                ):
                    return await self.show(owner, state, ident)
                value["ready"] = await self.service.check(owner, value)
                self.store.update_draft(ident, owner, draft["revision"], value)
            elif command == "policy":
                draft = self.service.load(owner, ident)
                if draft["state"] == "DRAFT":
                    payload = draft["payload"]
                    _, policy, _ = PromotionSettings(self.store, self.api).get()
                    if payload["promotion_policy"] != asdict(policy):
                        payload["promotion_policy"] = asdict(policy)
                        payload.pop("ready", None)
                        self.store.update_draft(ident, owner, draft["revision"], payload)
                return await self.show(owner, state, ident, advanced=True)
            elif command != "open":
                raise Conflict("Экран запуска устарел")
            return await self.show(owner, state, ident)
        if action == "ln:policy:apply":
            proposal = (await state.get_data()).get("launch_policy_proposal")
            if not proposal:
                raise Conflict("Предложение настройки устарело. Откройте настройки продвижения заново")
            draft = self.service.load(owner, proposal["draft_id"])
            if draft["state"] != "DRAFT":
                raise Conflict("Черновик запуска уже закрыт")
            return await self.shop_settings(state, draft["id"])
        parts = action.split(":")
        view = (await state.get_data()).get("launch", {})
        if len(parts) < 3 or view.get("nonce") != parts[1]:
            raise Conflict("Кнопка настройки устарела. Откройте черновик запуска заново")
        draft = self.service.load(owner, view["id"])
        if draft["revision"] != view["revision"] or draft["state"] != "DRAFT":
            raise Conflict("Настройки уже изменены или запуск подтверждён")
        value, command = draft["payload"], parts[2]
        p = f"ln:{parts[1]}:"
        if command in {"advanced", "basic"}:
            return await self.show(owner, state, draft["id"], advanced=command == "advanced")
        if command == "shop":
            return await self.shop_settings(state, draft["id"])
        if command == "edit" and parts[3:] in (["budget"], ["schedule"]):
            group = "budget" if parts[3] == "budget" else "time"
            return await self.shop_settings(state, draft["id"], group=group)
        if command == "modes":
            return "⚡ Выберите выдачу до публикации.", keyboard(
                [[(label, p + "mode:" + mode)] for mode, label in MODES.items()],
                back=("Назад", "launch:open:" + draft["id"]),
            )
        if command == "mode":
            if parts[3] not in MODES:
                raise ValidationError("Неизвестный способ выдачи")
            previous = value["mode"]
            value["mode"] = parts[3]
            if parts[3] == "playerok":
                from app.services.logical_products import LogicalProducts

                product = LogicalProducts(self.store, self.api).listing(value["launch_target"])
                if (
                    product
                    and product["handler"] == "telegram_gifts"
                    and not json.loads(product["config"]).get("platform_only")
                ):
                    value["platform_switch_confirmed"] = False
                    value["prior_mode"] = previous
                    value.pop("ready", None)
                    self.store.update_draft(draft["id"], owner, draft["revision"], value)
                    await state.update_data(launch={**view, "revision": draft["revision"] + 1})
                    return (
                        "Сменить Telegram Gifts на выдачу Playerok? Бот больше не будет покупать подарки для этого товара. Необходима подтверждённая выдача площадкой.",
                        keyboard(
                            [[("Подтвердить смену способа", p + "platform_switch")]],
                            back=("Отмена", p + "platform_cancel"),
                        ),
                    )
            if previous != parts[3]:
                value.update(
                    auto_complete=parts[3] != "manual",
                    inherit=parts[3] != "manual",
                    platform_reply=False,
                    refund=False,
                )
            value["manual_delivery_confirmed"] = parts[3] == "manual"
        elif command == "platform_switch":
            if value["mode"] != "playerok":
                raise Conflict("Выбран другой способ выдачи")
            value["platform_switch_confirmed"] = True
            value.update(auto_complete=True, inherit=True, platform_reply=False, refund=False)
            value.pop("prior_mode", None)
        elif command == "platform_cancel":
            if value["mode"] != "playerok" or value.get("platform_switch_confirmed"):
                raise Conflict("Выбор уже изменён. Откройте способ выдачи заново")
            value["mode"] = value.pop("prior_mode", "gifts")
        elif command == "platform_reply":
            if value["mode"] != "playerok":
                raise Conflict("Сообщение после оплаты доступно в режиме Playerok")
            value["platform_reply"] = not value.get("platform_reply", False)
        elif command in {"complete", "refund", "promotion", "inherit"}:
            key = "auto_complete" if command == "complete" else command
            value[key] = not value.get(key, False)
        elif command == "edit":
            field = parts[3]
            if field not in FIELDS:
                raise ValidationError("Поле недоступно")
            await state.set_state("launch:" + field)
            prompt = (
                "Сообщение после подтверждённой оплаты для товара, который выдаёт Playerok. «-» — стандартный текст."
                if field == "greeting" and value["mode"] == "playerok"
                else FIELDS[field]
            )
            if field in {"budget", "schedule"}:
                prompt = (
                    "🌍 Общие настройки всего магазина. Применяются ко всем товарам, а не только к этому лоту.\n"
                    + prompt
                )
            return prompt, keyboard([], back=("Отмена", "launch:open:" + draft["id"]))
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
            else:
                # Paid consent needs a fresh, visible exact quote. Ordinary
                # navigation never implicitly prepares financial authority.
                value.pop("ready", None)
                self.store.update_draft(draft["id"], owner, draft["revision"], value)
                draft = self.service.load(owner, draft["id"])
                context = (await state.get_data())["launch"]
                await state.update_data(launch={**context, "revision": draft["revision"]})
                value["ready"] = await self.service.check(owner, value)
                self.store.update_draft(draft["id"], owner, draft["revision"], value)
                return await self.show(owner, state, draft["id"])
        elif command in {"catalog", "gift", "giftundo", "giftclear"}:
            if command == "catalog" and not self.store.rows("SELECT 1 FROM gift_catalog LIMIT 1"):
                await self.service.gifts.refresh()
            chosen = list(
                value.get("gift_selection", gift_codes(value["gift_code"]) if value.get("gift_code") else [])
            )
            if command == "gift":
                if (
                    not self.store.rows(
                        "SELECT 1 FROM gift_catalog WHERE code=? AND available=1", (parts[3],)
                    )
                    or len(chosen) >= self.service.gifts.settings.max_gifts
                ):
                    raise ValidationError("Подарок недоступен или превышено допустимое количество")
                chosen.append(parts[3])
            elif command == "giftundo":
                chosen = chosen[:-1]
            elif command == "giftclear":
                chosen = []
            value["gift_selection"] = chosen
            self.store.update_draft(draft["id"], owner, draft["revision"], value)
            draft = self.service.load(owner, draft["id"])
            nonce = secrets.token_hex(6)
            await state.update_data(launch={"id": draft["id"], "revision": draft["revision"], "nonce": nonce})
            p = f"ln:{nonce}:"
            entries = self.store.rows("SELECT code,stars FROM gift_catalog WHERE available=1 ORDER BY code")
            rows = [[(f"{gift_name(g['code'])} · {g['stars']} ⭐", p + "gift:" + g["code"])] for g in entries]
            if chosen:
                rows += [
                    [("Убрать последний", p + "giftundo"), ("Очистить выбор", p + "giftclear")],
                    [("Сохранить состав в черновик Playerok", p + "savegifts")],
                ]
            return "🎁 Выбрано: " + (
                ", ".join(gift_name(code) for code in chosen) or "пока ничего"
            ), keyboard(rows, back=("К запуску", "launch:open:" + draft["id"]))
        elif command == "savegifts":
            chosen = value.get("gift_selection", [])
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
            from app.telegram.logical_products_panel import LogicalProductsPanel

            panel = LogicalProductsPanel(self.store, self.api)
            prepared = await panel.service.prepare(
                owner, item["id"], "telegram_gifts", {"code": ", ".join(chosen)}
            )
            value["gift_code"] = ", ".join(chosen)
            value.pop("ready", None)
            self.store.update_draft(draft["id"], owner, draft["revision"], value)
            await panel.card(owner, item["id"], state)
            view = (await state.get_data())["logical_view"]
            await state.update_data(logical_prepared=prepared, logical_launch_id=draft["id"])
            return panel.preview(prepared, view)
        elif command == "check":
            value["ready"] = await self.service.check(owner, value)
            self.store.update_draft(draft["id"], owner, draft["revision"], value)
            return await self.show(owner, state, draft["id"])
        elif command == "commit":
            if not value.get("ready") and value["publication"] == "DEFAULT":
                value["ready"] = await self.service.check(owner, value)
                self.store.update_draft(draft["id"], owner, draft["revision"], value)
                draft = self.service.load(owner, draft["id"])
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
        else:
            raise Conflict("Поле настройки устарело")
        value.pop("ready", None)
        self.store.update_draft(draft["id"], message.from_user.id, draft["revision"], value)
        return await self.show(message.from_user.id, state, draft["id"])
