import secrets

from app.database.store import encode
from app.playerok.adapter import validate_page
from app.services.products import ValidationError
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard


class ChatPanel:
    """Opaque cursors stay in durable FSM; callbacks contain only a view nonce."""

    def __init__(self, store, api):
        self.store, self.api = store, api

    async def show(self, action, state):
        if action.startswith("chat:"):
            ident = action.split(":", 1)[1]
            view = {"id": ident, "cursors": [None], "index": 0}
        else:
            parts = action.split(":")
            view = (await state.get_data()).get("chat_view", {})
            if len(parts) != 3 or parts[1] != view.get("nonce"):
                raise ValidationError("Страница диалога устарела. Откройте диалог заново.")
            if parts[2] == "older" and view.get("next"):
                index = view["index"] + 1
                view["cursors"] = view["cursors"][:index] + [view["next"]]
                view["index"] = index
            elif parts[2] == "newer" and view["index"] > 0:
                view["index"] -= 1
            else:
                raise ValidationError("Недоступная страница диалога")
            ident = view["id"]
        cursor = view["cursors"][view["index"]]
        page = await self.api.call("get_chat_messages", chat_id=ident, count=8, after_cursor=cursor)
        messages, following = validate_page(page, "messages", view["cursors"][: view["index"] + 1])
        for message in messages:
            # Browsing old history is not an incoming event and must not notify owners.
            self.store.execute(
                "INSERT INTO chat_messages VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (message["id"], ident, message.get("created_at"), encode(message)),
            )
        view.update(next=following, nonce=secrets.token_hex(6))
        await state.update_data(chat_view=view)
        blocks = []
        for message in reversed(messages):
            user = message.get("user") or {}
            author = escaped(user.get("username") or "Событие", 60)
            date = escaped(message.get("created_at") or "", 32)
            text = "[сообщение удалено]" if message.get("deleted_at") else message.get("text") or "[событие]"
            images = message.get("images") or []
            attachment = f"\n📷 Изображений: {len(images)}" if images else ""
            blocks.append(f"<b>{author}</b> · {date}\n{escaped(text, 290)}{attachment}")
        rows = [[("✉️ Ответить", "reply:" + ident), ("✅ Прочитано", "readchat:" + ident)]]
        navigation = []
        if view["index"]:
            navigation.append(("← Новее", f"chatpage:{view['nonce']}:newer"))
        if following:
            navigation.append(("Старее →", f"chatpage:{view['nonce']}:older"))
        if navigation:
            rows.append(navigation)
        rows.append([("🔄 Последние", "chat:" + ident)])
        return (
            f"💬 <b>Диалог · страница {view['index'] + 1}</b>\n\n"
            + ("\n\n".join(blocks) if blocks else "Сообщений нет."),
            keyboard(rows),
        )
