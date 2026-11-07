import secrets
from dataclasses import asdict

from app.database.store import Conflict
from app.services.automation import Automation, message_text
from app.services.delivery_chains import DeliveryChains
from app.services.logical_products import LogicalProducts
from app.services.products import ValidationError
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.presentation import item_label, order_label

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
            keyboard(buttons, back=("⬅️ Обслуживание выдачи", "tools:delivery")),
        )

    async def callback(self, action, owner, state):
        self.service.orders.owner(owner)
        if action == "automation":
            return await self.menu(owner, state)
        if action == "auto:stop" or action.startswith("auto:stop_confirm:"):
            from app.telegram.stop_confirmation import StopConfirmation

            confirmation = StopConfirmation(self.store, self.service.api)
            if action == "auto:stop":
                return await confirmation.request("auto", owner, state)
            count = await confirmation.confirm(
                "auto", owner, state, action.split(":", 2)[2], self.service.stop_all
            )
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
            ident = parts[2]
            revision, policy = self.service.get(ident)
            if policy.mode == "playerok":
                from app.telegram.platform_panel import PlatformPanel

                return await PlatformPanel(self.store, self.service.api).callback(
                    "platform:item:" + ident, owner, state
                )
            from app.telegram.logical_products_panel import LogicalProductsPanel

            return await LogicalProductsPanel(self.store, self.service.api).card(owner, ident, state)
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
        if data.get("id") and not data.get("logical_check"):
            from app.telegram.logical_products_panel import LogicalProductsPanel

            return await LogicalProductsPanel(self.store, self.service.api).card(owner, data["id"], state)
        if len(parts) not in {3, 4} or parts[1] != data.get("nonce"):
            raise Conflict("Настройки устарели. Откройте товар заново")
        command, values = parts[2], data["values"]
        if command == "orders":
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
        await state.update_data(automation=data)
        from app.telegram.logical_products_panel import LogicalProductsPanel

        return await LogicalProductsPanel(self.store, self.service.api).card(
            message.from_user.id, data["id"], state
        )
