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
        text += "Товар уже выдаётся самой площадкой. Бот не отправляет его повторно, а может автоматически ответить покупателю и отметить выполнение заказа."
        text += "\n\nАвтоответ после оплаты: " + ("🟢 Включён" if data["reply"] else "выключен")
        text += "\nАвтовыполнение заказа: " + ("🟢 Включено" if data["fulfill"] else "выключено")
        text += "\nОтвет: «" + escaped(data["text"], 600) + "»"
        text += "\nСохранение разрешает выбранные действия для будущих покупок. Обязательный отчёт остаётся ручным действием."
        return text, keyboard(
            [
                [("💬 Автоответ: " + ("Вкл" if data["reply"] else "Выкл"), prefix + "reply")],
                [("✏️ Изменить текст ответа", prefix + "text")],
                [("✅ Автовыполнение: " + ("Вкл" if data["fulfill"] else "Выкл"), prefix + "fulfill")],
                [("💾 Сохранить", prefix + "save")],
            ],
            back=("⬅️ К выдаче товара", "iv:delivery:" + data["id"]),
        )

    async def callback(self, action, owner, state):
        self.service.policies.orders.owner(owner)
        parts = action.split(":")
        if len(parts) == 3 and parts[1] == "item":
            await self.service.policies.item(owner, parts[2])
            revision, policy = self.service.policies.get(parts[2])
            selected = policy.mode == "playerok"
            return await self.show(
                {
                    "id": parts[2],
                    "revision": revision,
                    "reply": selected and policy.paid_reply and policy.enabled,
                    "fulfill": selected and policy.mark_sent and policy.enabled,
                    "text": policy.greeting if selected else DEFAULT_REPLY,
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
            await self.service.save(
                owner,
                data["id"],
                data["revision"],
                reply=data["reply"],
                fulfill=data["fulfill"],
                text=data["text"],
            )
            data["revision"], _ = self.service.policies.get(data["id"])
            return await self.show(data, state, saved=True)
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
