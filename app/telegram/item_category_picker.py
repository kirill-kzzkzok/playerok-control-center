import secrets

from app.playerok.adapter import APIError, validate_page
from app.services.item_filters import checked_filters
from app.services.products import ValidationError
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard


class ItemCategoryPicker:
    def __init__(self, api):
        self.api = api

    async def start(self, state):
        data = await state.get_data()
        await state.set_data(
            {
                "item_filters": checked_filters(data.get("item_filters", {})),
                **{key: True for key in ("gift_picker", "promotion_picker") if data.get(key)},
            }
        )
        await state.set_state("itemlist:category_search")
        return "📂 Введите название игры для поиска категории Playerok.", keyboard(
            (
                [[("Снять фильтр категории", "il:clearcategory")]]
                if data.get("item_filters", {}).get("category_id")
                else []
            )
            + [[("Отмена", "itemfilters")]]
        )

    async def search(self, text, state, *, view=None):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 100:
            raise ValidationError("Название игры: от 1 до 100 символов")
        view = view or {"search": text.strip(), "seen": [], "next": None, "page": 0}
        if view["page"] >= 1000:
            raise ValidationError("Достигнут лимит страниц поиска")
        page = await self.api.call("get_games", name=view["search"], count=10, after_cursor=view["next"])
        games, following = validate_page(page, "games", view["seen"])
        choices = self.choices(games)
        nonce = secrets.token_hex(6)
        view = {
            "search": view["search"],
            "seen": view["seen"] + ([following] if following else []),
            "next": following,
            "page": view["page"] + 1,
            "kind": "games",
            "choices": choices,
            "nonce": nonce,
        }
        rows = [[(entry["name"][:60], f"icp:{nonce}:pick:{index}")] for index, entry in enumerate(choices)]
        if following:
            rows.append([("Далее →", f"icp:{nonce}:next:0")])
        rows.append([("Отмена", "itemfilters")])
        await state.update_data(category_picker=view)
        return "📂 <b>Игра</b> · " + escaped(view["search"], 100) + (
            "\nВыберите игру или введите новый поиск."
            if choices
            else "\nНа этой странице игр нет. Можно ввести другой поиск."
        ), keyboard(rows)

    def choices(self, values):
        if (
            not isinstance(values, list)
            or len(values) > 1000
            or any(
                not isinstance(entry, dict)
                or not isinstance(entry.get("id"), str)
                or not entry["id"]
                or len(entry["id"]) > 100
                or not isinstance(entry.get("name"), str)
                or not entry["name"]
                for entry in values
            )
        ):
            raise APIError("UPSTREAM_INCOMPLETE_RESPONSE")
        return [{"id": entry["id"], "name": entry["name"][:200]} for entry in values]

    async def game_categories(self, ident):
        game = await self.api.call("get_game", id=ident)
        if game.get("id") != ident:
            raise APIError("UPSTREAM_IDENTITY_MISMATCH")
        return self.choices(game.get("categories"))

    async def categories(self, state, view, page):
        choices = view["choices"]
        if not 0 <= page <= max(0, (len(choices) - 1) // 10):
            raise ValidationError("Страница категории недоступна")
        nonce = secrets.token_hex(6)
        rows = [
            [(entry["name"][:60], f"icp:{nonce}:pick:{index}")]
            for index, entry in enumerate(choices[page * 10 : page * 10 + 10], page * 10)
        ]
        nav = []
        if page:
            nav.append(("← Назад", f"icp:{nonce}:page:{page - 1}"))
        if (page + 1) * 10 < len(choices):
            nav.append(("Далее →", f"icp:{nonce}:page:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([("Другая игра", "il:pickcategory"), ("Отмена", "itemfilters")])
        await state.update_data(category_picker={**view, "nonce": nonce, "page": page})
        return "📂 <b>Категория</b> · страница " + str(page + 1) + (
            "\nВыберите категорию." if choices else "\nУ этой игры нет доступных категорий."
        ), keyboard(rows)

    async def action(self, action, state):
        parts = action.split(":")
        data = await state.get_data()
        view = data.get("category_picker", {})
        if (
            await state.get_state() != "itemlist:category_search"
            or len(parts) != 4
            or parts[1] != view.get("nonce")
        ):
            raise ValidationError("Выбор категории устарел")
        command = parts[2]
        if command == "next" and view["kind"] == "games" and view["next"]:
            return await self.search(view["search"], state, view=view)
        try:
            index = int(parts[3])
        except ValueError:
            raise ValidationError("Некорректная кнопка категории") from None
        if command == "page" and view["kind"] == "categories":
            return await self.categories(state, view, index)
        if command != "pick" or not 0 <= index < len(view["choices"]):
            raise ValidationError("Выбор категории недоступен")
        selected = view["choices"][index]
        if view["kind"] == "games":
            choices = await self.game_categories(selected["id"])
            return await self.categories(
                state, {"kind": "categories", "game_id": selected["id"], "choices": choices}, 0
            )
        # Refresh membership; the snapshot is navigation, not authority to apply an ID.
        fresh = await self.game_categories(view["game_id"])
        if not any(entry["id"] == selected["id"] for entry in fresh):
            raise ValidationError("Категория больше не доступна. Выберите игру заново")
        filters = checked_filters((await state.get_data()).get("item_filters", {}))
        filters["category_id"] = selected["id"]
        filters = checked_filters(filters)
        await state.set_data(
            {
                "item_filters": filters,
                **{key: True for key in ("gift_picker", "promotion_picker") if data.get(key)},
            }
        )
        await state.set_state(None)
        return None
