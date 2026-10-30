import secrets

from app.playerok.adapter import validate_page
from app.services.item_filters import checked_filters, matches_item
from app.services.products import ValidationError
from app.telegram.formatting import escaped
from app.telegram.item_category_picker import ItemCategoryPicker
from app.telegram.keyboards import keyboard
from playerokapi.enums import ItemStatuses


class ListingsPanel:
    def __init__(self, api):
        self.api = api
        self.category_picker = ItemCategoryPicker(api)

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
            filters = checked_filters(data.get("item_filters", {}))
            status = filters.get("status")
            extra = {"category_id": filters["category_id"]} if filters.get("category_id") else {}
            page = await self.api.call(
                "get_my_items",
                count=10,
                after_cursor=cursor,
                statuses=[ItemStatuses[status]] if status else None,
                **extra,
            )
            title, prefix = "📦 Мои товары", "item"
        else:
            page = await self.api.call("get_chats", count=10, after_cursor=cursor)
            title, prefix = "💬 Диалоги", "chat"
        values, next_cursor = validate_page(page, kind, seen)
        loaded = len(values)
        if kind == "items":
            values = [item for item in values if matches_item(item, filters)]
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
            rows.append([("🔎 Фильтры", "itemfilters")])
        # Save only after the whole response/buttons validate; errors leave retryable cursor.
        await state.update_data(
            list_kind=kind,
            list_next=next_cursor,
            list_nonce=next_nonce,
            list_seen=[*seen, next_cursor] if next_cursor else [],
            list_number=number,
        )
        empty = (
            "На этой странице записей нет; продолжение доступно."
            if next_cursor
            else "На этой странице записей нет."
        )
        note = ""
        if kind == "items":
            note = f"\nНа серверной странице: {loaded} · совпало: {len(values)}. Фильтрация постраничная."
            note += "\nФильтры: " + escaped(
                {key: value for key, value in filters.items() if value is not None and value != ""}, 500
            )
            if not values:
                empty += " Это не поиск по ещё не загруженным страницам."
        return f"<b>{title}</b> · страница {number}\n" + (
            "Выберите запись." if values else empty
        ) + note, keyboard(rows)

    async def filter_action(self, action, state):
        if action == "il:pickcategory":
            return await self.category_picker.start(state)
        data = await state.get_data()
        filters = checked_filters(data.get("item_filters", {}))
        if action == "il:reset":
            await state.clear()
            return await self.show("items", state)
        field = action.removeprefix("il:")
        if field in {"PREMIUM", "DEFAULT", "ALL"}:
            filters["priority"] = None if field == "ALL" else field
            await state.set_data({"item_filters": filters})
            return await self.show("items", state)
        prompts = {
            "search": "Название: от 1 до 200 символов; «-» очищает поиск.",
            "price": "Диапазон цены в ₽: минимум максимум, например 100 500. «-» означает открытую границу: - 500; - - очищает диапазон.",
            "category": "ID категории Playerok (не название): буквы, цифры, дефис/подчёркивание; «-» снимает фильтр. ID передаётся серверу, список ограничен вашим аккаунтом.",
        }
        if field not in prompts:
            raise ValidationError("Фильтр недоступен")
        await state.set_data({"item_filters": filters})
        await state.set_state("itemlist:" + field)
        return prompts[field], keyboard([[("Отмена", "itemfilters")]])

    async def filter_message(self, message, state):
        current = await state.get_state()
        filters = checked_filters((await state.get_data()).get("item_filters", {}))
        text = (message.text or "").strip()
        if current == "itemlist:category_search":
            return await self.category_picker.search(text, state)
        if current == "itemlist:search":
            if not text or len(text) > 200:
                raise ValidationError("Название: от 1 до 200 символов")
            filters["search"] = "" if text == "-" else text
        elif current == "itemlist:price":
            parts = text.split()
            if len(parts) != 2:
                raise ValidationError("Укажите две границы цены через пробел")
            filters.update(
                minimum=None if parts[0] == "-" else parts[0], maximum=None if parts[1] == "-" else parts[1]
            )
        elif current == "itemlist:category":
            filters["category_id"] = None if text == "-" else text
        else:
            raise ValidationError("Ввод фильтра устарел")
        filters = checked_filters(filters)
        await state.set_data({"item_filters": filters})
        await state.set_state(None)
        return await self.show("items", state)
