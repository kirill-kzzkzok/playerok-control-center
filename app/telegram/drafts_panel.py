from app.database.store import Conflict
from app.services.catalog import DRAFT_FILTERS, draft_page
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.storage import replace_context

DRAFT_LABELS = {
    "DRAFT": "Черновик",
    "QUEUED": "Ожидает выполнения",
    "REMOTE_DRAFT": "Создан на Playerok",
    "UPDATED": "Изменён",
    "CANCELLED": "Отменён",
}


class DraftsPanel:
    def __init__(self, store):
        self.store = store

    def owner(self, owner):
        if not self.store.rows("SELECT 1 FROM administrators WHERE user_id=?", (owner,)):
            raise Conflict("Доступ закрыт")

    async def listing(self, owner, state, page=0, selected="ALL", search=""):
        self.owner(owner)
        result = draft_page(self.store, owner, page, selected, search)
        await replace_context(state, None, {"draft_list_filter": selected, "draft_list_search": search})
        text = f"💾 <b>Локальные черновики · {DRAFT_FILTERS[selected][0]}</b>\nСтраница {result['page'] + 1}/{result['pages']} · Всего: {result['total']}"
        if search:
            text += "\nПоиск: " + escaped(search, 500)
        if not result["rows"]:
            text += "\nЗаписи не найдены."
        rows = [
            [
                (
                    (
                        str(row["name"] or "Новый товар")[:42]
                        + " · "
                        + DRAFT_LABELS.get(row["state"], "Откройте результат")
                    )[:64],
                    "draft:" + row["id"],
                )
            ]
            for row in result["rows"]
        ]
        navigation = []
        if result["has_previous"]:
            navigation.append(("← Назад", f"dl:page:{result['page'] - 1}:{selected}"))
        if result["has_next"]:
            navigation.append(("Далее →", f"dl:page:{result['page'] + 1}:{selected}"))
        if navigation:
            rows.append(navigation)
        rows.append([("➕ Создать товар", "create")])
        return text, keyboard(rows, back=("⬅️ Черновики", "itemfilter:DRAFT"))

    async def callback(self, action, owner, state):
        self.owner(owner)
        if action == "drafts":
            return await self.listing(owner, state)
        data = await state.get_data()
        search, selected = data.get("draft_list_search", ""), data.get("draft_list_filter", "ALL")
        if action == "dl:filters":
            return (
                "💾 <b>Статусы локальных записей</b>\nСостояния черновиков не являются текущими статусами объявления Playerok.",
                keyboard([[(label, "dl:filter:" + key)] for key, (label, _) in DRAFT_FILTERS.items()]),
            )
        if action.startswith("dl:filter:"):
            return await self.listing(owner, state, selected=action.split(":", 2)[2], search=search)
        if action.startswith("dl:page:"):
            parts = action.split(":")
            return await self.listing(owner, state, int(parts[2]), parts[3], search)
        if action == "dl:search":
            await replace_context(
                state, "draftlist:search", {"draft_list_filter": selected, "draft_list_search": search}
            )
            return (
                "Введите часть названия или ID черновика (1–200 символов). Данные выдачи и описания в поиск не входят.",
                keyboard([[("Отмена поиска", "dl:page:0:" + selected)]]),
            )
        raise ValueError("Действие списка черновиков недоступно")

    async def message(self, message, state):
        self.owner(message.from_user.id)
        if await state.get_state() != "draftlist:search":
            raise Conflict("Поиск устарел")
        value = (message.text or "").strip()
        if not 1 <= len(value) <= 200:
            raise ValueError("Поиск: от 1 до 200 символов")
        data = await state.get_data()
        return await self.listing(
            message.from_user.id, state, selected=data.get("draft_list_filter", "ALL"), search=value
        )
