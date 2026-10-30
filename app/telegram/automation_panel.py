import secrets
from dataclasses import asdict

from app.database.store import Conflict
from app.services.automation import Automation, DeliveryPolicy, message_text
from app.services.delivery_chains import DeliveryChains
from app.services.products import ValidationError
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard

FIELDS = {"text": "Данные постоянной выдачи", "greeting": "Приветствие", "after": "Сообщение после выдачи"}


class AutomationPanel:
    def __init__(self, store, api):
        self.service, self.store = Automation(store, api), store

    async def menu(self, owner, state):
        self.service.orders.owner(owner)
        await state.clear()
        rows = self.store.rows(
            "SELECT item_id,revision FROM delivery_policies ORDER BY updated_at DESC LIMIT 20"
        )
        buttons = [
            [(row["item_id"][:42] + " · v" + str(row["revision"]), "auto:item:" + row["item_id"])]
            for row in rows
        ]
        buttons.extend(
            [
                [("📦 Выбрать товар", "items")],
                [("🚀 Продвижение · dry-run", "promotions")],
                [("Все политики выдачи", "aq:list:policies:0"), ("Цепочки выдачи", "aq:list:chains:0")],
                [("Регистрация будущих заказов", "aq:list:enrollment:0")],
                [("⛔ Выключить все политики выдачи", "auto:stop")],
            ]
        )
        return (
            "⚡ <b>Автоматизация</b>\nЗдесь сохраняются политики и выполняются dry-run проверки. Можно подтвердить запуск отдельного заказа либо отдельно включить будущие продажи; для исполнения нужны разрешающие флаги в локальной конфигурации.\nАвтоподнятие: только расчёт, платные операции запрещены. Автовывод запрещён.\nВыберите товар в списке или откройте его карточку.",
            keyboard(buttons),
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
                [("Переключить запрос отметки SENT", f"ap:{nonce}:toggle:mark_sent")],
                [("Будущие заказы автоматически", f"ap:{nonce}:toggle:auto_enroll")],
                [("💾 Сохранить политику", f"ap:{nonce}:save")],
                [("🔎 Dry-run оплаченных заказов", f"ap:{nonce}:orders")],
            ]
        )
        return text, keyboard(rows)

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
        if len(parts) == 3 and parts[:2] == ["auto", "item"]:
            await self.service.item(owner, parts[2])
            revision, policy = self.service.get(parts[2])
            return await self.show({"id": parts[2], "revision": revision, "values": asdict(policy)}, state)
        data = (await state.get_data()).get("automation", {})
        if len(parts) not in {3, 4} or parts[1] != data.get("nonce"):
            raise Conflict("Настройки устарели. Откройте товар заново")
        command, values = parts[2], data["values"]
        if command == "mode" and len(parts) == 4 and parts[3] in {"manual", "text", "stock"}:
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
                "Выберите заказ для dry-run. Проверка читает API, не резервирует код и не создаёт отправки.",
                keyboard(
                    [
                        [(order["id"], f"ap:{data['nonce']}:simulate:{index}")]
                        for index, order in enumerate(orders)
                    ]
                ),
            )
        elif command == "simulate" and len(parts) == 4:
            index = int(parts[3])
            if not 0 <= index < len(data.get("orders", [])):
                raise ValidationError("Заказ недоступен")
            result = await self.service.simulate(owner, data["orders"][index])
            text = "🔎 <b>Dry-run</b>\nВыдача по условиям: " + (
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
                + [[("К политике", "auto:item:" + data["id"])]]
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
        field = data.get("field")
        if field not in FIELDS:
            raise ValidationError("Шаг ввода устарел")
        data["values"][field] = message_text(message.text or "", required=True)
        data.pop("field", None)
        return await self.show(data, state)
