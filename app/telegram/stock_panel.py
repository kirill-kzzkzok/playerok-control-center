import secrets
from dataclasses import asdict

from app.database.store import Conflict
from app.services.inventory import LABELS, Inventory
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard


class StockPanel:
    def __init__(self, store, api):
        self.store, self.service = store, Inventory(store, api)

    async def card(self, owner, ident):
        item = await self.service.automation.item(owner, ident)
        counts = self.service.counts(ident)
        rows = [[("➕ Загрузить запас", "stock:add:" + ident)]]
        rows += [[(f"{LABELS[s]}: {n}", f"stock:list:{ident}:{s}:0")] for s, n in counts.items()]
        for draft in self.service.prepared(owner, ident):
            rows.append([("📥 Перенести запас из мастера", "stock:transfer:" + draft)])
        guard = self.store.setting(f"delivery:{ident}:enabled")
        _, policy = self.service.automation.get(ident)
        note = ""
        if guard is False:
            note = "\n⛔ Выдача остановлена. Пополнение её не возобновляет."
            if counts["AVAILABLE"] and policy.enabled and policy.mode == "stock":
                rows.append([("Подготовить возобновление", "stock:resume:" + ident)])
        rows += [[("⚡ Настроить политику", "auto:item:" + ident)], [("📦 К товару", "item:" + ident)]]
        return (
            "📚 <b>Склад · "
            + escaped(item.get("name") or ident, 150)
            + "</b>\nID: <code>"
            + escaped(ident, 100)
            + "</code>\n"
            + "\n".join(f"{LABELS[s]}: {n}" for s, n in counts.items())
            + "\nСодержимое единиц скрыто. Выданные и карантинные значения повторно не используются."
            + note,
            keyboard(rows),
        )

    async def confirm(self, owner, state, ident, text, draft_id=None):
        item = await self.service.item(owner, ident)
        preview = self.service.preview(ident, text)
        nonce = secrets.token_hex(8)
        await state.set_state("stock:confirm")
        await state.set_data(
            {
                "stock_pending": {
                    "id": ident,
                    "text": text,
                    "preview": preview,
                    "status": item["status"],
                    "draft": draft_id,
                    "nonce": nonce,
                }
            }
        )
        return (
            f"📥 Добавить новых единиц: {preview['new']}\nПустых строк: {preview['empty']}\nДубликатов внутри сообщения: {preview['duplicates']}\nУже известных складу: {preview['known']}"
            + "\n\nПривязка: <code>"
            + escaped(ident, 100)
            + "</code>\nИзвестные значения пропускаются независимо от их статуса. Отправки и включения автоматики не будет.",
            keyboard(
                [[("✅ Сохранить в склад", "stock:commit:" + nonce)], [("Отмена", "stock:view:" + ident)]]
            ),
        )

    async def callback(self, action, owner, state):
        self.service.automation.orders.owner(owner)
        parts = action.split(":")
        command, ident = parts[1:3]
        if command == "view":
            await state.clear()
            return await self.card(owner, ident)
        if command == "add":
            await self.service.item(owner, ident)
            await state.set_state("stock:input")
            await state.set_data({"stock_item": ident})
            return (
                "Пришлите запас одним сообщением: одна единица на строку, до 10000 символов. Перед сохранением будет показано количество, без содержимого.",
                keyboard([[("Отмена", "stock:view:" + ident)]]),
            )
        if command == "transfer":
            draft = self.store.get_draft(ident, owner)
            binding = self.store.rows("SELECT remote_id FROM bindings WHERE draft_id=?", (ident,))
            if not binding or ident not in self.service.prepared(owner, binding[0]["remote_id"]):
                raise Conflict("Подготовленный запас недоступен или уже перенесён")
            return await self.confirm(
                owner, state, binding[0]["remote_id"], draft["payload"]["delivery_data"], ident
            )
        if command == "commit":
            pending = (await state.get_data()).get("stock_pending", {})
            if pending.get("nonce") != ident:
                raise Conflict("Подтверждение устарело или уже принято")
            count = await self.service.import_values(
                owner, pending["id"], pending["text"], pending["preview"], pending["status"], pending["draft"]
            )
            await state.clear()
            text, markup = await self.card(owner, pending["id"])
            return f"✅ Добавлено: {count}. Автоматизация не включалась.\n\n" + text, markup
        if command == "list":
            await self.service.automation.item(owner, ident)
            selected, page = parts[3], int(parts[4])
            if selected not in LABELS or not 0 <= page <= 100000:
                raise Conflict("Страница недоступна")
            rows = self.store.rows(
                "SELECT i.id,r.order_id FROM inventory i LEFT JOIN reservations r ON r.inventory_id=i.id WHERE i.item_id=? AND i.status=? ORDER BY i.id LIMIT 11 OFFSET ?",
                (ident, selected, page * 10),
            )
            text = f"📚 {LABELS[selected]} · страница {page + 1}\n" + (
                "\n".join(
                    f"Единица №{row['id']}" + (" · есть привязанный заказ" if row["order_id"] else "")
                    for row in rows[:10]
                )
                or "Единиц нет."
            )
            buttons = [
                [("Заказ единицы №" + str(row["id"]), "order:" + row["order_id"])]
                for row in rows[:10]
                if row["order_id"]
            ]
            nav = []
            if page:
                nav.append(("← Назад", f"stock:list:{ident}:{selected}:{page - 1}"))
            if len(rows) > 10:
                nav.append(("Далее →", f"stock:list:{ident}:{selected}:{page + 1}"))
            if nav:
                buttons.append(nav)
            return text, keyboard(buttons + [[("К складу", "stock:view:" + ident)]])
        if command == "resume":
            await self.service.item(owner, ident)
            revision, policy = self.service.automation.get(ident)
            if not policy.enabled or policy.mode != "stock" or not self.service.counts(ident)["AVAILABLE"]:
                raise Conflict("Сначала настройте политику и пополните доступный запас")
            nonce = secrets.token_hex(8)
            await state.set_data(
                {
                    "stock_resume": {
                        "id": ident,
                        "revision": revision,
                        "values": asdict(policy),
                        "nonce": nonce,
                    }
                }
            )
            await state.set_state(None)
            return (
                "Возобновить выдачу? Будет сохранена новая версия политики; прежние цепочки не перезапускаются. Автоматический охват начинается с новых будущих заказов. Глобальные разрешения не меняются.",
                keyboard(
                    [[("✅ Возобновить", "stock:resumeok:" + nonce)], [("Отмена", "stock:view:" + ident)]]
                ),
            )
        if command == "resumeok":
            pending = (await state.get_data()).get("stock_resume", {})
            if pending.get("nonce") != ident:
                raise Conflict("Подтверждение устарело")
            if not self.service.counts(pending["id"])["AVAILABLE"]:
                raise Conflict("Доступный запас закончился")
            await self.service.automation.save(
                owner, pending["id"], pending["revision"], pending["values"], future_confirmed=True
            )
            await state.clear()
            return await self.card(owner, pending["id"])
        raise Conflict("Действие склада недоступно")

    async def message(self, message, state):
        self.service.automation.orders.owner(message.from_user.id)
        data = await state.get_data()
        return await self.confirm(message.from_user.id, state, data["stock_item"], message.text)
