import secrets
from html import escape

from aiogram.types import BufferedInputFile

from app.services.attachments import Attachments
from app.services.products import ValidationError
from app.services.replies import Replies, reply_payload
from app.telegram.keyboards import keyboard


class RepliesPanel:
    def __init__(self, store):
        self.replies = Replies(store)
        self.attachments = Attachments()

    async def menu(self, owner, state, *, chat_id=None, origin=None):
        nonce = secrets.token_hex(6)
        await state.set_state("reply" if chat_id else None)
        await state.set_data(
            {
                "quick_view": {"nonce": nonce, "chat_id": chat_id, "origin_event": origin},
                "chat_id": chat_id,
                "origin_event": origin,
            }
        )
        values = self.replies.list(owner)
        rows = [[(value["title"], f"quick:{nonce}:view:{value['id']}")] for value in values]
        rows.append([("➕ Добавить ответ", f"quick:{nonce}:add")])
        if chat_id:
            rows.append([("✍️ Свой текст", f"quick:{nonce}:type")])
        return (
            "⚡ <b>Быстрые ответы</b>\n"
            + (
                "Введите ответ, пришлите фото с подписью или выберите шаблон. Перед отправкой будет подтверждение."
                if chat_id
                else "Здесь можно сохранить и изменить шаблоны ответов."
            ),
            keyboard(rows),
        )

    async def callback(self, action, owner, state):
        data = await state.get_data()
        view = data.get("quick_view", {})
        parts = action.split(":")
        if len(parts) not in {3, 4} or parts[1] != view.get("nonce"):
            raise ValidationError("Кнопка быстрых ответов устарела")
        command = parts[2]
        if command == "add":
            await state.set_state("quick:name")
            await state.update_data(quick_edit={})
            return "Введите название быстрого ответа (до 48 символов).", keyboard([])
        if command == "type" and view.get("chat_id"):
            await state.set_state("reply")
            await state.set_data({"chat_id": view["chat_id"], "origin_event": view.get("origin_event")})
            return "Введите ответ покупателю. Перед отправкой будет подтверждение.", keyboard([])
        if len(parts) != 4:
            raise ValidationError("Недоступное действие")
        value = self.replies.get(owner, parts[3])
        if command == "view":
            nonce = secrets.token_hex(6)
            view.update(nonce=nonce, selected=value["id"], revision=value["revision"])
            await state.update_data(quick_view=view)
            rows = []
            if view.get("chat_id"):
                rows.append([("✉️ Использовать", f"quick:{nonce}:use:{value['id']}")])
            rows.extend(
                [
                    [
                        ("✏️ Изменить", f"quick:{nonce}:edit:{value['id']}"),
                        ("🗑 Удалить", f"quick:{nonce}:delete:{value['id']}"),
                    ],
                    [("← К списку", f"quick:{nonce}:list:{value['id']}")],
                ]
            )
            return f"<b>{escape(value['title'])}</b>\n\n{escape(value['text'])}", keyboard(rows)
        if command == "list":
            return await self.menu(owner, state, chat_id=view.get("chat_id"), origin=view.get("origin_event"))
        if value["id"] != view.get("selected") or value["revision"] != view.get("revision"):
            raise ValidationError("Шаблон изменился. Выберите его заново")
        if command == "use" and view.get("chat_id"):
            return await self.confirm(state, view["chat_id"], value["text"], view.get("origin_event"))
        if command == "edit":
            await state.set_state("quick:text")
            await state.update_data(quick_edit=value)
            return "Пришлите новый текст ответа (до 3500 символов).", keyboard([])
        if command == "delete":
            nonce = secrets.token_hex(6)
            view["nonce"] = nonce
            await state.update_data(quick_view=view)
            return "Удалить сохранённый быстрый ответ?", keyboard(
                [[("Удалить", f"quick:{nonce}:remove:{value['id']}")]]
            )
        if command == "remove":
            self.replies.delete(owner, value["id"], value["revision"])
            return await self.menu(owner, state, chat_id=view.get("chat_id"), origin=view.get("origin_event"))
        raise ValidationError("Недоступное действие")

    async def message(self, message, state):
        data = await state.get_data()
        edit = data.get("quick_edit", {})
        text = message.text or ""
        if await state.get_state() == "quick:name":
            if not text.strip() or len(text.encode("utf-16-le")) // 2 > 48:
                raise ValidationError("Введите название от 1 до 48 символов")
            await state.update_data(quick_edit={"title": text.strip()})
            await state.set_state("quick:text")
            return "Введите текст быстрого ответа (до 3500 символов).", keyboard([])
        self.replies.save(
            message.from_user.id, edit["title"], text, ident=edit.get("id"), revision=edit.get("revision")
        )
        view = data.get("quick_view", {})
        return await self.menu(
            message.from_user.id, state, chat_id=view.get("chat_id"), origin=view.get("origin_event")
        )

    async def photo(self, message, state):
        data = await state.get_data()
        file = message.photo[-1] if message.photo else message.document
        source = await self.attachments.download(message.bot, file)
        try:
            result = await self.confirm(
                state, data["chat_id"], message.caption or "", data.get("origin_event"), photos=[source]
            )
        except BaseException:
            self.attachments.discard(source)
            raise
        await message.answer_photo(
            BufferedInputFile(await self.attachments.read(source), filename="reply-preview.jpg"),
            caption="Предпросмотр фотографии. Подтвердите отправку следующим сообщением.",
        )
        return result

    @staticmethod
    async def confirm(state, chat_id, text, origin=None, *, photos=None):
        payload = reply_payload({"text": text, "photos": photos or []})
        nonce = secrets.token_hex(8)
        await state.set_state(None)
        await state.set_data(
            {
                "pending": {
                    "kind": "reply",
                    "target": chat_id,
                    "payload": payload,
                    "origin_event": origin,
                },
                "confirm_token": nonce,
            }
        )
        return "Отправить покупателю?\n\n" + escape(payload["text"]) + (
            f"\n📷 Фотографий: {len(photos)}" if photos else ""
        ), keyboard([[("✅ Отправить", "commit:" + nonce)]], back=("Отмена", "chat:" + chat_id))
