import secrets

from app.database.store import Conflict
from app.services.automation import message_text
from app.services.platform_orders import DEFAULT_REPLY, PlatformOrders
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard


class PlatformPanel:
    def __init__(self, store, api):
        self.service = PlatformOrders(store, api)

    async def show(self, data, state, *, saved=False):
        data["nonce"] = secrets.token_hex(6)
        await state.set_state(None)
        await state.set_data({"platform": data})
        prefix = "platform:" + data["nonce"] + ":"
        text = "📦 <b>Выдача: Playerok</b>\n"
        if saved:
            text += "✅ Сохранено для будущих покупок.\n"
        elif data.get("configured") and not data.get("active"):
            text += "⏸ Выполнение остановлено. Сохранённые настройки показаны ниже.\n"
        if not saved:
            _, policy = self.service.policies.get(data["id"])
            if (
                data["reply"] != (policy.mode == "playerok" and policy.paid_reply)
                or data["fulfill"] != (policy.mode == "playerok" and policy.mark_sent)
                or data["saved_text"] is not None
                and data["text"] != data["saved_text"]
            ):
                text += "✏️ Не сохранено. Нажмите «Сохранить», чтобы применить изменения.\n"
        product = self.service.logical.listing(data["id"])
        if product and product["state"] == "VERIFIED":
            text += "✅ Привязка подтверждена\n🔐 Постоянный код: " + escaped(product["code"], 20) + "\n"
        text += "Товар уже выдаётся самой площадкой. Бот не отправляет его повторно, а может автоматически ответить покупателю и отметить выполнение заказа."
        text += "\n\nАвтоответ после оплаты: " + ("🟢 Включён" if data["reply"] else "выключен")
        text += "\nАвтовыполнение заказа: " + ("🟢 Включено" if data["fulfill"] else "выключено")
        text += "\nОтвет: «" + escaped(data["text"], 600) + "»"
        text += "\nСохранение разрешает выбранные действия для будущих покупок и наследование при штатном перевыставлении. Обязательный отчёт остаётся ручным действием."
        return text, keyboard(
            [
                [("💬 Автоответ: " + ("Вкл" if data["reply"] else "Выкл"), prefix + "reply")],
                [("✏️ Изменить текст ответа", prefix + "text")],
                [("✅ Автовыполнение: " + ("Вкл" if data["fulfill"] else "Выкл"), prefix + "fulfill")],
                [("💾 Сохранить", prefix + "save")],
                [("🔄 Изменить способ выдачи", "lp:method:" + data["id"])],
            ],
            back=("⬅️ К товару", "item:" + data["id"]),
        )

    async def save(self, data, owner, state, *, switching=False):
        result = await self.service.save(
            owner,
            data["id"],
            data["revision"],
            reply=data["reply"],
            fulfill=data["fulfill"],
            text=data["text"],
            switch_confirmed=switching,
            expected_text=data.get("saved_text"),
        )
        data["nonce"] = secrets.token_hex(6)
        data["pending"] = result
        await state.set_state(None)
        await state.set_data({"platform": data})
        if result.get("switch"):
            return (
                "⚠️ Сейчас настроена Telegram Gifts. Заменить её на «Выдаёт Playerok»? Бот перестанет отправлять подарки по этому товару.",
                keyboard(
                    [[("✅ Подтвердить замену Telegram Gifts", "platform:" + data["nonce"] + ":switch")]],
                    back=("⬅️ Отмена", "platform:item:" + data["id"]),
                ),
            )
        if result.get("binding"):
            return (
                "🔐 Будет добавлен постоянный код товара.\nЭтот код позволит находить настройки при перевыставлении и изменении ID сделки.\nНастройки включатся только после подтверждения Playerok.",
                keyboard(
                    [[("✅ Подтвердить привязку и настройки", "platform:" + data["nonce"] + ":bind")]],
                    back=("⬅️ Отмена", "platform:item:" + data["id"]),
                ),
            )
        if result.get("operation"):
            return self.waiting(data["id"], result["operation"])
        data.pop("pending", None)
        data["revision"], _ = self.service.policies.get(data["id"])
        data["saved_text"] = data["text"]
        return await self.show(data, state, saved=True)

    def waiting(self, item, operation):
        rows = self.service.store.rows("SELECT status,error_code FROM outbox WHERE id=?", (operation,))
        status = rows[0]["status"] if rows else None
        text = "⏳ Проверяем сохранение постоянного кода. Новая автоматизация ещё не включена."
        if status == "UNCERTAIN" or rows and rows[0]["error_code"] == "MANUALLY_STOPPED_UNCERTAIN":
            text = "⚠️ Результат изменения описания неизвестен. Повторная запись запрещена; требуется проверка кода."
        elif status == "PERMANENT_FAILED":
            text = "⚠️ Не удалось изменить описание. Проверьте разрешение редактирования на Playerok и откройте настройки заново."
        return text, keyboard(
            [
                [("🔄 Проверить состояние", "platform:item:" + item)],
                [("Результат операции", "result:show:" + operation)],
                [("Проверка привязки без повторной записи", "lp:item:" + item)],
            ],
            back=("⬅️ К товару", "item:" + item),
        )

    async def callback(self, action, owner, state):
        self.service.policies.orders.owner(owner)
        parts = action.split(":")
        if len(parts) == 3 and parts[1] == "item":
            await self.service.policies.item(owner, parts[2])
            product = self.service.logical.listing(parts[2])
            if product and product["state"] == "PENDING":
                operation = self.service.store.rows(
                    "SELECT status,started_at FROM outbox WHERE id=?", (product["operation_id"],)
                )
                if (
                    not operation
                    or operation[0]["status"] != "PERMANENT_FAILED"
                    or operation[0]["started_at"] is not None
                ):
                    return self.waiting(parts[2], product["operation_id"])
            revision, policy = self.service.policies.get(parts[2])
            selected = policy.mode == "playerok"
            active = (
                selected
                and policy.enabled
                and bool(product and product["enabled"] and product["state"] == "VERIFIED")
            )
            return await self.show(
                {
                    "id": parts[2],
                    "revision": revision,
                    "reply": selected and policy.paid_reply,
                    "fulfill": selected and policy.mark_sent,
                    "configured": selected,
                    "active": active,
                    "text": policy.greeting if selected else DEFAULT_REPLY,
                    "saved_text": policy.greeting if selected else None,
                },
                state,
            )
        data = (await state.get_data()).get("platform", {})
        if len(parts) != 3 or parts[1] != data.get("nonce"):
            raise Conflict("Настройки устарели. Откройте выдачу товара заново")
        command = parts[2]
        if command in {"reply", "fulfill"}:
            data[command] = not data[command]
        elif command == "text":
            await state.set_state("platform:text")
            return "✏️ Пришлите новый ответ покупателю.\nТекущий: «" + escaped(
                data["text"], 3500
            ) + "»", keyboard([], back=("⬅️ Отмена", "platform:item:" + data["id"]))
        elif command == "save":
            return await self.save(data, owner, state)
        elif command == "switch" and data.get("pending", {}).get("switch"):
            return await self.save(data, owner, state, switching=True)
        elif command == "bind" and data.get("pending", {}).get("binding"):
            result = self.service.confirm(owner, data["pending"])
            await state.clear()
            return self.waiting(data["id"], result["operation_id"])
        else:
            raise Conflict("Действие устарело. Откройте выдачу товара")
        return await self.show(data, state)

    async def message(self, message, state):
        self.service.policies.orders.owner(message.from_user.id)
        data = (await state.get_data()).get("platform")
        if not data:
            raise Conflict("Откройте настройки выдачи заново")
        data["text"] = message_text(message.text, required=True)
        return await self.show(data, state)
