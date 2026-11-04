import json
import secrets
import time

from app.database.store import now
from app.playerok.adapter import APIError, validate_page
from app.services.finance import quote_fresh
from app.services.item_filters import checked_filters, matches_item
from app.services.products import ValidationError
from app.telegram.item_category_picker import ItemCategoryPicker
from app.telegram.keyboards import keyboard
from app.telegram.presentation import chat_peer, local_time, short_title
from playerokapi.enums import ItemStatuses


class ListingsPanel:
    def __init__(self, api, store=None):
        self.api, self.store = api, store
        self.category_picker = ItemCategoryPicker(api)

    async def search_page(self, filters, state, cursor):
        account = getattr(self.api.account, "id", None)
        if not account:
            raise ValidationError("Подтвердите аккаунт Playerok перед поиском")
        key = "ui:catalog:" + account
        cache = self.store.setting(key, {}) if self.store else {}
        if not quote_fresh(cache.get("at"), max_age=60):
            if cursor:
                raise ValidationError("Снимок каталога устарел. Выполните поиск заново")
            cache = {"at": now(), "items": [], "cursor": None, "seen": [], "complete": False}
        if not cache["complete"]:
            indexed = {item["id"]: item for item in cache["items"]}
            started = time.monotonic()
            for _ in range(3):
                if time.monotonic() - started >= 5:
                    break
                try:
                    page = await self.api.call(
                        "get_my_items", count=24, after_cursor=cache["cursor"], statuses=None
                    )
                    values, continuation = validate_page(page, "items", cache["seen"])
                except APIError:
                    cache["error"] = True
                    break
                for value in values:
                    item = {
                        k: value.get(k) for k in ("id", "name", "price", "status", "priority", "category")
                    }
                    if self.store:
                        code = self.store.rows(
                            "SELECT p.code FROM logical_products p JOIN logical_listings l ON l.product_id=p.id WHERE l.item_id=? AND p.account_id=?",
                            (value["id"], account),
                        )
                    else:
                        code = []
                    from app.services.logical_products import marker

                    try:
                        item["search_code"] = code[0]["code"] if code else marker(value.get("name") or "")
                    except ValueError:
                        item["search_code"] = None
                    indexed[item["id"]] = item
                cache.update(cursor=continuation, error=False, complete=not continuation)
                if continuation:
                    cache["seen"].append(continuation)
                else:
                    break
            cache["items"] = list(indexed.values())
            if self.store:
                self.store.set_setting(key, cache)
        data = await state.get_data()
        if cursor and (not str(cursor).startswith("catalog:") or data.get("list_catalog_at") != cache["at"]):
            raise ValidationError("Результаты поиска изменились. Откройте поиск заново")
        offset = int(cursor.split(":")[1]) if cursor else 0
        matches = [
            item
            for item in cache["items"]
            if matches_item(item, filters)
            and (
                not filters.get("category_id")
                or (item.get("category") or {}).get("id") == filters["category_id"]
            )
        ]
        if self.store and (filters.get("delivery") or filters.get("problems")):
            delivery = {
                row["item_id"]
                for row in self.store.rows(
                    "SELECT l.item_id FROM logical_listings l JOIN logical_products p ON p.id=l.product_id WHERE p.account_id=? AND p.handler!='manual' "
                    "UNION SELECT item_id FROM gift_listings UNION SELECT item_id FROM delivery_policies WHERE json_extract(payload,'$.mode')!='manual'",
                    (account,),
                )
            }
            problems = {
                row["item_id"]
                for row in self.store.rows(
                    "SELECT l.item_id FROM logical_listings l JOIN logical_products p ON p.id=l.product_id WHERE p.account_id=? AND (p.error_code IS NOT NULL OR p.state='BLOCKED') "
                    "UNION SELECT item_id FROM gift_jobs WHERE state IN ('BLOCKED','UNCERTAIN','INVALID_RECIPIENT') "
                    "UNION SELECT target AS item_id FROM outbox WHERE status IN ('UNCERTAIN','PERMANENT_FAILED') AND COALESCE(error_code,'') NOT IN ('MANUALLY_STOPPED','OWNER_STOPPED')",
                    (account,),
                )
            }
            matches = [
                item
                for item in matches
                if (not filters.get("delivery") or item["id"] in delivery)
                and (not filters.get("problems") or item["id"] in problems)
            ]
        await state.update_data(list_catalog_at=cache["at"])
        next_cursor = f"catalog:{offset + 10}" if len(matches) > offset + 10 else None
        return (
            {
                "items": matches[offset : offset + 10],
                "page_info": {"has_next_page": bool(next_cursor), "end_cursor": next_cursor},
            },
            cache,
            len(matches),
        )

    async def show(self, kind, state, *, nonce=None, restore=False):
        if kind not in {"items", "chats"}:
            raise ValidationError("Неизвестный список")
        data = await state.get_data()
        gift_picker = kind == "items" and data.get("gift_picker") is True
        promotion_picker = kind == "items" and data.get("promotion_picker") is True
        cursor, seen, number = None, [], 1
        if restore and data.get("list_kind") == kind:
            cursor, number = data.get("list_cursor"), data.get("list_number", 1)
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
            filters = checked_filters(
                {"status": None if gift_picker else "APPROVED", **data.get("item_filters", {})}
            )
            status = filters.get("status")
            extra = {"category_id": filters["category_id"]} if filters.get("category_id") else {}
            search = bool(filters.get("search") or filters.get("delivery") or filters.get("problems"))
            if search:
                page, search_cache, found = await self.search_page(filters, state, cursor)
            else:
                page = await self.api.call(
                    "get_my_items",
                    count=10,
                    after_cursor=cursor,
                    statuses=(
                        [value for value in ItemStatuses if value.name not in {"APPROVED", "DRAFT"}]
                        if filters.get("archive")
                        else [ItemStatuses[status]]
                        if status
                        else None
                    ),
                    **extra,
                )
            title, prefix = "📦 Мои товары", "item"
            if promotion_picker:
                title, prefix = "🚀 Выберите товар для продвижения", "payp:item"
            if gift_picker:
                title, prefix = "🎁 Выберите товар для подарков", "gft:item"
        else:
            page = await self.api.call("get_chats", count=10, after_cursor=cursor)
            title, prefix = "💬 Диалоги", "chat"
        values, next_cursor = validate_page(page, kind, seen)
        if kind == "items":
            values = [item for item in values if matches_item(item, filters)]
            if (
                self.store
                and not cursor
                and filters.get("status") == "APPROVED"
                and not any(value for key, value in filters.items() if key != "status")
            ):
                self.store.set_setting(
                    "shop:active:" + str(self.api.account.id),
                    {
                        "count": None if next_cursor else sum(not item.get("deleted_at") for item in values),
                        "at": now(),
                    },
                )
        # An empty page with continuation still offers Next; it does not mean EOF.
        rows = [[("➕ Создать товар", "create")]] if kind == "items" and not promotion_picker else []
        for value in values:
            label = (
                value.get("name") or "Товар без названия"
                if kind == "items"
                else chat_peer(value, getattr(getattr(self.api, "account", None), "id", None))
            )
            if kind == "items":
                if self.store is not None and isinstance(value.get("name"), str):
                    self.store.set_setting("ui:item:" + value["id"] + ":name", value["name"][:200])
                label = f"{label} · {value.get('price', '?')} ₽"
                if filters.get("status") == "DRAFT" and not gift_picker and not promotion_picker:
                    label = "☁️ Playerok · " + label
            elif value.get("unread_messages_counter"):
                label = f"🔵 {value['unread_messages_counter']} · {label}"
            if kind == "chats":
                if self.store is not None:
                    self.store.set_setting(
                        "ui:chat:" + value["id"] + ":name",
                        chat_peer(value, getattr(getattr(self.api, "account", None), "id", None)),
                    )
                last = value.get("last_message") or {}
                if not last and self.store is not None:
                    cached = self.store.rows(
                        "SELECT payload FROM chat_messages WHERE chat_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                        (value["id"],),
                    )
                    last = json.loads(cached[0]["payload"]) if cached else {}
                if last:
                    snippet = (
                        "Фото" if last.get("images") and not last.get("text") else last.get("text") or ""
                    )
                    if last.get("deleted_at"):
                        snippet = "Сообщение удалено"
                    stamp = local_time(last.get("created_at"), clock=True)
                    label = short_title(label, 25) + " · " + short_title(snippet, 20, "Событие")
                    if stamp != "дата не указана":
                        label += " · " + stamp
            callback = f"{prefix}:{value['id']}"
            if len(callback.encode("utf-8")) > 64:
                raise ValidationError("Идентификатор записи не помещается в кнопку Telegram")
            rows.append([(str(label)[:60], callback)])
        next_nonce = secrets.token_hex(6) if next_cursor else None
        has_local = False
        if next_cursor:
            rows.append([("Далее →", f"listnext:{kind}:{next_nonce}")])
        if kind == "items":
            if filters.get("status") == "DRAFT" and not gift_picker and not promotion_picker and self.store:
                owner = state.key.user_id
                local = self.store.rows(
                    "SELECT id,json_extract(payload,'$.name') AS name FROM drafts "
                    "WHERE owner_id=? AND state IN ('DRAFT','QUEUED') "
                    "AND json_extract(payload,'$.launch_target') IS NULL "
                    "AND json_extract(payload,'$.edit_target') IS NULL "
                    "AND NOT EXISTS(SELECT 1 FROM bindings b WHERE b.draft_id=drafts.id) "
                    "ORDER BY created_at DESC,id DESC LIMIT 6",
                    (owner,),
                )
                local_rows = [
                    [("💾 Локально · " + str(d["name"] or "Новый товар")[:40], "draft:" + d["id"])]
                    for d in local[:5]
                ]
                has_local = bool(local_rows)
                if len(local) > 5:
                    local_rows.append([("Все локальные черновики", "drafts")])
                rows[1:1] = local_rows
                title = "💾 Черновики · локально и на Playerok"
            rows.extend(
                [
                    [("Активные", "itemfilter:APPROVED"), ("Архив и прочие", "itemfilter:ARCHIVE")],
                    [("💾 Черновики", "itemfilter:DRAFT")],
                ]
            )
        # Save only after the whole response/buttons validate; errors leave retryable cursor.
        await state.update_data(
            list_kind=kind,
            list_cursor=cursor,
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
        note = "\nФрагменты — по сохранённой истории." if kind == "chats" else ""
        if gift_picker:
            note = "\nВыберите созданный на Playerok товар, включая серверные черновики. Локальный черновик сначала нужно создать на Playerok через мастер."
        if kind == "items" and search:
            note += f"\nСовпало: {found} по снимку каталога · {local_time(search_cache['at'])}."
            if not search_cache["complete"]:
                note += " Поиск не завершён: часть каталога ещё не проверена."
                rows.append([("Продолжить поиск", "il:scan")])
            rows.append([("🔄 Обновить каталог", "il:refreshsearch")])
        elif kind == "items" and any(v is not None and v != "" for v in filters.values()):
            note += f"\nСовпало: {len(values)}. Фильтрация постраничная."
            if not values:
                note += " По ещё не загруженным страницам поиск не выполнен."
        return f"<b>{title}</b> · страница {number}\n" + (
            "" if values or has_local else empty
        ) + note, keyboard(
            rows,
            back=("⬅️ Товары с подарками", "gft:bindings")
            if gift_picker
            else ("К автопродвижению", "promotions")
            if promotion_picker
            else ("⬅️ Мой магазин", "home"),
        )

    async def filter_action(self, action, state):
        if action in {"il:scan", "il:refreshsearch"}:
            if action == "il:refreshsearch" and self.store:
                self.store.set_setting("ui:catalog:" + self.api.account.id, {})
            return await self.show("items", state)
        if action in {"il:delivery", "il:problems"}:
            filters = checked_filters((await state.get_data()).get("item_filters", {}))
            key = action.split(":")[1]
            filters.update({key: not filters.get(key, False), "status": None, "archive": False})
            await state.update_data(item_filters=filters)
            return await self.show("items", state)
        if action == "il:pickcategory":
            return await self.category_picker.start(state)
        data = await state.get_data()
        if action == "il:remember":
            owner = state.key.user_id
            self.store.set_setting(f"ui:{owner}:item_filters", data.get("item_filters", {}))
            self.store.set_setting(f"ui:{owner}:item_filters_pinned", True)
            return "✅ Фильтр сохранён для открытия товаров.", keyboard([[("К списку", "il:back")]])
        filters = checked_filters(data.get("item_filters", {}))
        if action == "il:reset":
            await state.clear()
            await state.set_data({key: True for key in ("gift_picker", "promotion_picker") if data.get(key)})
            return await self.show("items", state)
        field = action.removeprefix("il:")
        if field in {"PREMIUM", "DEFAULT", "ALL"}:
            filters["priority"] = None if field == "ALL" else field
            await state.set_data(
                {
                    "item_filters": filters,
                    **{key: True for key in ("gift_picker", "promotion_picker") if data.get(key)},
                }
            )
            return await self.show("items", state)
        prompts = {
            "search": "Название: от 1 до 200 символов; «-» очищает поиск.",
            "price": "Диапазон цены в ₽: минимум максимум, например 100 500. «-» означает открытую границу: - 500; - - очищает диапазон.",
            "category": "ID категории Playerok (не название): буквы, цифры, дефис/подчёркивание; «-» снимает фильтр. ID передаётся серверу, список ограничен вашим аккаунтом.",
        }
        if field not in prompts:
            raise ValidationError("Фильтр недоступен")
        await state.set_data(
            {
                "item_filters": filters,
                **{key: True for key in ("gift_picker", "promotion_picker") if data.get(key)},
            }
        )
        await state.set_state("itemlist:" + field)
        return prompts[field], keyboard([[("Отмена", "itemfilters")]])

    async def filter_message(self, message, state):
        current = await state.get_state()
        data = await state.get_data()
        filters = checked_filters(data.get("item_filters", {}))
        text = (message.text or "").strip()
        if current == "itemlist:category_search":
            return await self.category_picker.search(text, state)
        if current == "itemlist:search":
            if not text or len(text) > 200:
                raise ValidationError("Название: от 1 до 200 символов")
            filters["search"] = "" if text == "-" else text
            filters.update(status=None, archive=False)
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
        await state.set_data(
            {
                "item_filters": filters,
                **{key: True for key in ("gift_picker", "promotion_picker") if data.get(key)},
            }
        )
        await state.set_state(None)
        return await self.show("items", state)
