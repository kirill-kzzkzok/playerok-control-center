import json
import secrets

from app.database.store import encode
from app.playerok.adapter import validate_page
from app.services.attachments import Attachments
from app.services.products import ValidationError
from app.telegram.formatting import escaped, html_pages
from app.telegram.keyboards import keyboard
from app.telegram.presentation import local_time


class ChatPanel:
    """Opaque cursors stay in durable FSM; callbacks contain only a view nonce."""

    def __init__(self, store, api):
        self.store, self.api = store, api
        self.attachments = Attachments()

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
            elif parts[2] in {"textnext", "textprev"}:
                view["text_page"] = view.get("text_page", 0) + (1 if parts[2] == "textnext" else -1)
            elif parts[2] == "newer" and view["index"] > 0:
                view["index"] -= 1
            else:
                raise ValidationError("Недоступная страница диалога")
            if parts[2] in {"older", "newer"}:
                view["text_page"] = 0
            ident = view["id"]
        cursor = view["cursors"][view["index"]]
        account = getattr(getattr(self.api, "account", None), "id", None)
        owner = state.key.user_id
        local = bool(
            account
            and self.store.setting("playerok:account_id") == account
            and (
                view.get("local") is True
                and view.get("account") == account
                or self.store.rows(
                    "SELECT 1 FROM chat_cards WHERE chat_id=? AND owner_id=? AND account_id=?",
                    (ident, owner, account),
                )
                or self.store.rows(
                    "SELECT 1 FROM orders WHERE json_extract(payload,'$.chat.id')=? AND json_extract(payload,'$.item.user.id')=?",
                    (ident, account),
                )
            )
        )
        if "local" in view:
            local = (
                view["local"]
                and view.get("account") == account
                and self.store.setting("playerok:account_id") == account
            )
        view["local"] = bool(local)
        if local:
            if "anchor" not in view:
                latest = self.store.rows(
                    "SELECT COALESCE(created_at,'') AS at,id FROM chat_messages WHERE chat_id=? ORDER BY COALESCE(created_at,'') DESC,id DESC LIMIT 1",
                    (ident,),
                )
                view["anchor"] = [latest[0]["at"], latest[0]["id"]] if latest else ["", ""]
            saved = self.store.rows(
                "SELECT payload FROM chat_messages WHERE chat_id=? AND (COALESCE(created_at,''),id)<=(?,?) ORDER BY COALESCE(created_at,'') DESC,id DESC LIMIT 9 OFFSET ?",
                (ident, *view["anchor"], view["index"] * 8),
            )
            messages = [json.loads(row["payload"]) for row in saved[:8]]
            following = str(view["index"] + 1) if len(saved) > 8 else None
            from app.workers.sync import request_sync

            request_sync(self.store, "chat", ident)
        else:
            page = await self.api.call("get_chat_messages", chat_id=ident, count=8, after_cursor=cursor)
            messages, following = validate_page(page, "messages", view["cursors"][: view["index"] + 1])
        if account:
            view["account"] = account
        for message in messages:
            # Browsing old history is not an incoming event and must not notify owners.
            self.store.execute(
                "INSERT INTO chat_messages VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (message["id"], ident, message.get("created_at"), encode(message)),
            )
        media = []
        for message in reversed(messages):
            if not message.get("deleted_at"):
                media.extend(
                    {"message_id": message["id"], "image_index": index}
                    for index, _ in enumerate(message.get("images") or [])
                )
        view.update(next=following, nonce=secrets.token_hex(6), media=media)
        await state.update_data(chat_view=view)
        peer_name = self.store.setting("ui:chat:" + ident + ":name")
        if not peer_name or peer_name == "Собеседник не указан":
            peers = {
                (entry.get("user") or {}).get("username")
                for entry in messages
                if (entry.get("user") or {}).get("id")
                and (entry.get("user") or {}).get("id")
                != getattr(getattr(self.api, "account", None), "id", None)
                and (entry.get("user") or {}).get("username")
            }
            peer_name = next(iter(peers)) if len(peers) == 1 else "собеседником"
        blocks = []
        for message in reversed(messages):
            user = message.get("user") or {}
            author = str(user.get("username") or "Событие")[:60]
            date = local_time(message.get("created_at"))
            from app.services.chat_events import system_event

            text = (
                "[сообщение удалено]"
                if message.get("deleted_at")
                else system_event(message) or message.get("text") or "[событие]"
            )
            images = message.get("images") or []
            attachment = f"\n📷 Изображений: {len(images)}" if images else ""
            blocks.append(f"{author} · {date}\n{text}{attachment}")
        pages = html_pages("\n\n".join(blocks) if blocks else "Сообщений нет.", 2800)
        text_page = view.get("text_page", 0)
        if not 0 <= text_page < len(pages):
            raise ValidationError("Страница текста устарела. Откройте диалог заново")
        await state.update_data(chat_view=view)
        rows = [[("✉️ Ответить", "compose:" + ident), ("✖️ Закрыть", "chatclose:" + ident)]]
        navigation = []
        if following:
            navigation.append(("Старые сообщения", f"chatpage:{view['nonce']}:older"))
        if view["index"]:
            navigation.append(("Новые сообщения", f"chatpage:{view['nonce']}:newer"))
        if navigation:
            rows.append(navigation)
        if len(pages) > 1:
            text_nav = []
            if text_page:
                text_nav.append(("← Текст", f"chatpage:{view['nonce']}:textprev"))
            if text_page + 1 < len(pages):
                text_nav.append(("Текст →", f"chatpage:{view['nonce']}:textnext"))
            rows.append(text_nav)
        if media:
            rows.append([("📷 Открыть вложения", f"chatmedia:{view['nonce']}:0")])
        freshness = ""
        if local:
            synced = self.store.rows("SELECT last_synced_at FROM sync_chats WHERE id=?", (ident,))
            freshness = (
                "Сохранённая история. Последняя сверка: "
                + (
                    local_time(synced[0]["last_synced_at"])
                    if synced and synced[0]["last_synced_at"]
                    else "ожидается"
                )
                + ". Обновляется в фоне.\n\n"
            )
        return (
            f"💬 <b>Переписка с покупателем</b>\n👤 {escaped(peer_name, 80)}\nстраница {view['index'] + 1}\n\n"
            + freshness
            + pages[text_page],
            keyboard(rows, home=False),
        )

    async def media(self, action, state):
        parts = action.split(":")
        view = (await state.get_data()).get("chat_view", {})
        if len(parts) != 3 or parts[1] != view.get("nonce"):
            raise ValidationError("Просмотр фото устарел. Откройте диалог заново")
        try:
            index = int(parts[2])
            if index < 0:
                raise ValueError
            selected = view["media"][index]
        except (ValueError, KeyError, IndexError):
            raise ValidationError("Фотография недоступна") from None
        page = await self.api.call(
            "get_chat_messages", chat_id=view["id"], count=8, after_cursor=view["cursors"][view["index"]]
        )
        messages, _ = validate_page(page, "messages")
        message = next((message for message in messages if message["id"] == selected["message_id"]), None)
        if not message or message.get("deleted_at"):
            raise ValidationError("Сообщение удалено или изменилось. Обновите диалог")
        images = message.get("images") or []
        if selected["image_index"] >= len(images):
            raise ValidationError("Вложение изменилось. Обновите диалог")
        image = images[selected["image_index"]]
        if not isinstance(image, dict):
            raise ValidationError("API вернул неполное вложение")
        data = await self.attachments.remote(image.get("url"))
        rows = []
        if index:
            rows.append(("← Фото", f"chatmedia:{view['nonce']}:{index - 1}"))
        if index + 1 < len(view["media"]):
            rows.append(("Фото →", f"chatmedia:{view['nonce']}:{index + 1}"))
        return (
            data,
            f"📷 Фотография {index + 1} из {len(view['media'])}",
            keyboard(([rows] if rows else []) + [[("💬 К переписке", "chat:" + view["id"])]]),
        )
