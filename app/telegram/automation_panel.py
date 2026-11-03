import secrets
from dataclasses import asdict

from app.database.store import Conflict
from app.services.automation import Automation, DeliveryPolicy, message_text
from app.services.delivery_chains import DeliveryChains
from app.services.logical_products import LogicalProducts
from app.services.products import ValidationError
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.presentation import automation_summary, item_label, order_label

FIELDS = {"text": "Данные постоянной выдачи", "greeting": "Приветствие", "after": "Сообщение после выдачи"}


class AutomationPanel:
    def __init__(self, store, api):
        self.service, self.store = Automation(store, api), store

    async def menu(self, owner, state):
        self.service.orders.owner(owner)
        await state.clear()
        rows = LogicalProducts(self.store, self.service.api).overview(owner, limit=6)
        buttons = [[(item_label(self.store, row["item_id"])[:55], "item:" + row["item_id"])] for row in rows]
        buttons.extend(
            [
                [("📦 Выбрать товар", "items")],
                [("Все товары с настройками", "aq:list:policies:0")],
                [("Проблемы выдачи", "attention"), ("Дополнительные инструменты", "tools:delivery")],
                [("⛔ Остановить автовыдачу", "auto:stop")],
            ]
        )
        return (
            "⚡ <b>Автовыдача</b>\n"
            + (
                "Глобальные разрешения включены. Готовность зависит от товара."
                if getattr(self.service.api.account, "writes_enabled", False)
                and getattr(self.service.api.account, "automation_enabled", False)
                else "Глобальные разрешения выключены — отправки не запускаются."
            )
            + "\nВыберите товар. Проверка без отправки доступна в его настройках.",
            keyboard(buttons, back=("⬅️ Ещё", "more")),
        )

    async def show(self, data, state):
        data.pop("arm_pending", None)
        nonce = secrets.token_hex(6)
        data["nonce"] = nonce
        await state.set_state(None)
        await state.set_data({"automation": data})
        values = data["values"]
        modes = {"manual": "Ручной", "text": "Постоянный текст", "stock": "Уникальный запас"}
        text = f"⚡ <b>Политика выдачи · v{data['revision']}</b>\nТовар: {escaped(data['id'], 100)}\nРежим: {modes[values['mode']]}\nЖелаемое включение: {'да' if values['enabled'] else 'нет'}\nОтметка SENT после выдачи: {'запрошена' if values['mark_sent'] else 'нет'}\n\nСохранение политики не запускает отправки. Запуск выбранного заказа подтверждается отдельно. Изменения применяются только после «Сохранить»."
        if values.get("auto_enroll") and values["enabled"]:
            text = text.replace(
                "Сохранение политики не запускает отправки. Запуск выбранного заказа подтверждается отдельно.",
                "После отдельного подтверждения будущие продажи могут запускаться автоматически при разрешённых глобальных флагах.",
            )
        for field, label in FIELDS.items():
            text += f"\n{label}: {'задано' if values[field] else 'пусто'}"
        text += "\n\nСохранённое состояние (правки выше ещё не применены):\n" + escaped(
            automation_summary(self.store, self.service.api, data["id"]), 1600
        )
        text += "\nАвтоматически включать будущие продажи: " + ("да" if values.get("auto_enroll") else "нет")
        text += "\nПри отдельном включении будущих продаж и двух разрешающих флагах новые заказы могут отправляться без подтверждения каждого. Старые заказы не включаются; смена версии политики начинает новый временной отсчёт."
        rows = [
            [
                ("Ручной", f"ap:{nonce}:mode:manual"),
                ("Текст", f"ap:{nonce}:mode:text"),
                ("Запас", f"ap:{nonce}:mode:stock"),
            ],
            [("Переключить желаемое включение", f"ap:{nonce}:toggle:enabled")],
        ]
        rows.extend(
            [
                [(label, f"ap:{nonce}:edit:{field}"), ("Очистить", f"ap:{nonce}:clear:{field}")]
                for field, label in FIELDS.items()
            ]
        )
        rows.extend(
            [
                [("Ожидать подтверждения владельца после выдачи", f"ap:{nonce}:toggle:mark_sent")],
                [("📚 Склад", "stock:view:" + data["id"])],
                [("Будущие заказы автоматически", f"ap:{nonce}:toggle:auto_enroll")],
                [("💾 Сохранить политику", f"ap:{nonce}:save")],
                [("🔎 Проверка без отправки оплаченных заказов", f"ap:{nonce}:orders")],
            ]
        )
        if not data.get("advanced"):
            text = "⚡ <b>Настройка выдачи</b>\n" + automation_summary(
                self.store, self.service.api, data["id"]
            )
            text += (
                "\nВыбрано: "
                + modes[values["mode"]]
                + (" · включение запрошено" if values["enabled"] else " · выключено")
            )
            text += "\nИзменения применяются после сохранения."
            _, saved = self.service.get(data["id"])
            if asdict(saved) != values:
                text += " Есть несохранённые изменения."
            rows = [
                rows[0],
                rows[1],
                [("📚 Склад", "stock:view:" + data["id"])],
                [("Дополнительные параметры", f"ap:{nonce}:advanced")],
                [("💾 Сохранить", f"ap:{nonce}:save")],
                [("🔎 Проверить без отправки", f"ap:{nonce}:orders")],
            ]
        else:
            rows.append([("⬅️ Основные настройки", f"ap:{nonce}:basic")])
        return text, keyboard(rows, back=("⬅️ К выдаче товара", "iv:delivery:" + data["id"]))

    async def callback(self, action, owner, state):
        self.service.orders.owner(owner)
        if action == "automation":
            return await self.menu(owner, state)
        if action == "auto:stop":
            count = self.service.stop_all(owner)
            await state.clear()
            return (
                f"Выключено политик: {count}. Уже поставленные вручную операции не отменяются; проверьте диагностику.",
                keyboard([[("Автоматизация", "automation")]]),
            )
        parts = action.split(":")
        if len(parts) == 3 and parts[:2] == ["auto", "check"]:
            await self.service.item(owner, parts[2])
            product = LogicalProducts(self.store, self.service.api).listing(parts[2])
            if not product or product["handler"] not in {"fixed_text", "unique_stock"}:
                raise ValidationError("Проверка доступна для текстовой или складской выдачи товара")
            revision, policy = self.service.get(parts[2])
            data = {
                "id": parts[2],
                "revision": revision,
                "values": asdict(policy),
                "nonce": secrets.token_hex(6),
                "logical_check": True,
            }
            await state.clear()
            await state.set_data({"automation": data})
            return await self.callback(f"ap:{data['nonce']}:orders", owner, state)
        if len(parts) == 3 and parts[:2] == ["auto", "item"]:
            if self.service.get(parts[2])[1].mode == "playerok":
                from app.telegram.platform_panel import PlatformPanel

                return await PlatformPanel(self.store, self.service.api).callback(
                    "platform:item:" + parts[2], owner, state
                )
            if LogicalProducts(self.store, self.service.api).listing(parts[2]):
                from app.telegram.logical_products_panel import LogicalProductsPanel

                return await LogicalProductsPanel(self.store, self.service.api).card(owner, parts[2], state)
            await self.service.item(owner, parts[2])
            revision, policy = self.service.get(parts[2])
            return await self.show({"id": parts[2], "revision": revision, "values": asdict(policy)}, state)
        data = (await state.get_data()).get("automation", {})
        if (
            data.get("id")
            and LogicalProducts(self.store, self.service.api).listing(data["id"])
            and not (
                data.get("logical_check")
                and len(parts) in {3, 4}
                and parts[2] in {"orders", "simulate", "plan", "start_chain"}
            )
        ):
            from app.telegram.logical_products_panel import LogicalProductsPanel

            return await LogicalProductsPanel(self.store, self.service.api).card(owner, data["id"], state)
        if len(parts) not in {3, 4} or parts[1] != data.get("nonce"):
            raise Conflict("Настройки устарели. Откройте товар заново")
        command, values = parts[2], data["values"]
        if command in {"advanced", "basic"}:
            data["advanced"] = command == "advanced"
        elif command == "mode" and len(parts) == 4 and parts[3] in {"manual", "text", "stock"}:
            values["mode"] = parts[3]
            if parts[3] == "manual":
                values["enabled"] = False
        elif command == "toggle" and len(parts) == 4 and parts[3] in {"enabled", "mark_sent", "auto_enroll"}:
            values[parts[3]] = not values[parts[3]]
        elif command in {"edit", "clear"} and len(parts) == 4 and parts[3] in FIELDS:
            if command == "clear":
                values[parts[3]] = ""
            else:
                data["field"] = parts[3]
                await state.update_data(automation=data)
                await state.set_state("automation:text")
                return (
                    FIELDS[parts[3]]
                    + ": пришлите текст до 3500 символов. Значение сохранится локально и не будет отправлено покупателю. Для отмены откройте главное меню.",
                    keyboard([]),
                )
        elif command == "save":
            DeliveryPolicy(**values)
            if values["enabled"] and values.get("auto_enroll"):
                data["nonce"] = secrets.token_hex(6)
                data["arm_pending"] = True
                await state.update_data(automation=data)
                return (
                    "Включить выдачу для будущих продаж этого товара? При PLAYEROK_WRITES_ENABLED и PLAYEROK_AUTOMATION_ENABLED новые оплаченные заказы будут обрабатываться без отдельного подтверждения. Заказы, созданные до сохранения этой версии, не включаются. Новая версия отменяет ранее активные цепочки по прежним правилам. Данные будут отправляться покупателю; проверьте их до включения.",
                    keyboard([[("✅ Сохранить и включить будущие продажи", f"ap:{data['nonce']}:arm")]]),
                )
            data["revision"] = await self.service.save(owner, data["id"], data["revision"], values)
        elif command == "arm" and data.get("arm_pending"):
            data["revision"] = await self.service.save(
                owner, data["id"], data["revision"], values, future_confirmed=True
            )
            data.pop("arm_pending", None)
        elif command == "orders":
            revision, policy = self.service.get(data["id"])
            if revision != data["revision"] or asdict(policy) != values:
                raise ValidationError("Сначала сохраните изменения или откройте политику заново")
            orders = self.store.rows(
                "SELECT id FROM orders WHERE item_id=? AND status='PAID' ORDER BY created_at DESC LIMIT 20",
                (data["id"],),
            )
            data["orders"] = [order["id"] for order in orders]
            data["nonce"] = secrets.token_hex(6)
            await state.update_data(automation=data)
            return (
                "Выберите заказ для проверки без отправки. Проверка читает API, не резервирует код и не создаёт отправки.",
                keyboard(
                    [
                        [(order_label(self.store, order["id"]), f"ap:{data['nonce']}:simulate:{index}")]
                        for index, order in enumerate(orders)
                    ],
                    back=("К выдаче товара", "lp:item:" + data["id"]) if data.get("logical_check") else None,
                ),
            )
        elif command == "simulate" and len(parts) == 4:
            index = int(parts[3])
            if not 0 <= index < len(data.get("orders", [])):
                raise ValidationError("Заказ недоступен")
            result = await self.service.simulate(owner, data["orders"][index])
            text = "🔎 <b>Проверка без отправки</b>\nВыдача по условиям: " + (
                "допустима" if result["would_deliver"] else "заблокирована"
            )
            text += "\nПричины: " + escaped("; ".join(result["reasons"]) or "нет", 1000)
            text += "\nШаги: " + escaped(", ".join(result["steps"]) or "нет", 300)
            text += "\nВ этой проверке отправок: 0. Резервов: 0. Автоматического запуска заказов нет."
            data["selected_order"] = data["orders"][index]
            data["evaluated_revision"] = result["policy_revision"]
            await state.update_data(automation=data)
            return text, keyboard(
                (
                    [[("Подготовить запуск цепочки", f"ap:{data['nonce']}:plan")]]
                    if result["would_deliver"]
                    else []
                )
                + [[("Открыть заказ", "order:" + data["selected_order"])]]
                + [
                    [
                        (
                            "К настройке выдачи",
                            ("lp:item:" if data.get("logical_check") else "auto:item:") + data["id"],
                        )
                    ]
                ]
            )
        elif command == "plan" and data.get("selected_order"):
            if not self.service.api.account.writes_enabled or not getattr(
                self.service.api.account, "automation_enabled", False
            ):
                raise ValidationError(
                    "Цепочки выключены: нужны PLAYEROK_WRITES_ENABLED и PLAYEROK_AUTOMATION_ENABLED в локальном .env"
                )
            data["nonce"] = secrets.token_hex(6)
            await state.update_data(automation=data)
            return (
                "Запустить сохранённую цепочку для выбранного оплаченного заказа? Приветствие, выдача и сообщение после выдачи отправятся по очереди с повторной проверкой сделки. Неизвестный результат остановит продолжение. Отметка SENT потребует отдельного fulfillment-сценария.",
                keyboard([[("✅ Запустить цепочку", f"ap:{data['nonce']}:start_chain")]]),
            )
        elif command == "start_chain" and data.get("selected_order"):
            ident = await DeliveryChains(self.store, self.service.api).create(
                owner, data["selected_order"], data["evaluated_revision"]
            )
            await state.clear()
            return ("Цепочка сохранена." if ident else "Доступный запас пуст. Цепочка не создана."), keyboard(
                [[("Заказ", "order:" + data["selected_order"])]]
            )
        else:
            raise ValidationError("Действие настройки недоступно")
        return await self.show(data, state)

    async def message(self, message, state):
        self.service.orders.owner(message.from_user.id)
        data = (await state.get_data())["automation"]
        if LogicalProducts(self.store, self.service.api).listing(data["id"]):
            from app.telegram.logical_products_panel import LogicalProductsPanel

            return await LogicalProductsPanel(self.store, self.service.api).card(
                message.from_user.id, data["id"], state
            )
        field = data.get("field")
        if field not in FIELDS:
            raise ValidationError("Шаг ввода устарел")
        data["values"][field] = message_text(message.text or "", required=True)
        data.pop("field", None)
        return await self.show(data, state)
