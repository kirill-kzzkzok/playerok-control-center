import secrets

from app.playerok.adapter import validate_page
from app.services.products import ValidationError
from app.telegram.keyboards import keyboard
from playerokapi.enums import ItemStatuses


class ListingsPanel:
    def __init__(self, api):
        self.api = api

    async def show(self, kind, state, *, nonce=None):
        if kind not in {"items", "chats"}:
            raise ValidationError("Неизвестный список")
        data = await state.get_data()
        cursor, seen, number = None, [], 1
        if nonce is not None:
            if (
                data.get("list_kind") != kind
                or not isinstance(nonce, str)
                or not secrets.compare_digest(nonce, data.get("list_nonce") or "")
                or not data.get("list_next")
            ):
                raise ValidationError("Список устарел. Откройте его заново")
            cursor = data["list_next"]
            seen = data.get("list_seen", [])
            number = data.get("list_number", 1) + 1
            if number > 1000:
                raise ValidationError("Достигнут лимит страниц. Откройте список заново")
        if kind == "items":
            status = data.get("item_filters", {}).get("status")
            if status and status not in ItemStatuses.__members__:
                raise ValidationError("Неизвестный статус товара")
            page = await self.api.call(
                "get_my_items",
                count=10,
                after_cursor=cursor,
                statuses=[ItemStatuses[status]] if status else None,
            )
            title, prefix = "📦 Мои товары", "item"
        else:
            page = await self.api.call("get_chats", count=10, after_cursor=cursor)
            title, prefix = "💬 Диалоги", "chat"
        values, next_cursor = validate_page(page, kind, seen)
        # An empty page with continuation still offers Next; it does not mean EOF.
        rows = []
        for value in values:
            user = value.get("user") or {}
            label = value.get("name") or user.get("username") or value["id"]
            if kind == "items":
                label = f"{value.get('price', '?')} ₽ · {label}"
            elif value.get("unread_messages_counter"):
                label = f"🔵 {value['unread_messages_counter']} · {label}"
            callback = f"{prefix}:{value['id']}"
            if len(callback.encode("utf-8")) > 64:
                raise ValidationError("Идентификатор записи не помещается в кнопку Telegram")
            rows.append([(str(label)[:60], callback)])
        next_nonce = secrets.token_hex(6) if next_cursor else None
        if next_cursor:
            rows.append([("Далее →", f"listnext:{kind}:{next_nonce}")])
        if kind == "items":
            rows.append([("🔎 Статусы", "itemfilters")])
        # Save only after the whole response/buttons validate; errors leave retryable cursor.
        await state.update_data(
            list_kind=kind,
            list_next=next_cursor,
            list_nonce=next_nonce,
            list_seen=[*seen, next_cursor] if next_cursor else [],
            list_number=number,
        )
        empty = "На этой странице записей нет; продолжение доступно." if next_cursor else "Записей нет."
        return f"<b>{title}</b> · страница {number}\n" + ("Выберите запись." if values else empty), keyboard(
            rows
        )
