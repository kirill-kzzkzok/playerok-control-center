"""Automatic owner-authorized migration and read-only outcome inspection."""

import json

from app.database.store import Conflict
from app.services.code_migration import CodeMigration
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.storage import replace_context

LABELS = {
    "REQUIRED": "Требуется перенос",
    "PLANNED": "Проверено",
    "PENDING": "Ожидает записи",
    "PROCESSING": "Выполняется",
    "VERIFIED": "Подтверждено",
    "BLOCKED": "Требует исправления",
    "UNCERTAIN": "Результат неизвестен — нужна сверка",
    "MANUAL_REVIEW_REQUIRED": "Архив требует ручного решения",
    "SUCCEEDED": "Подтверждено",
    "PERMANENT_FAILED": "Остановлено",
}


class CodeMigrationPanel:
    def __init__(self, store, api):
        self.store, self.service = store, CodeMigration(store, api)

    def home(self, owner):
        account = self.service.logical.authority(owner)
        products = self.store.rows(
            "SELECT p.id,p.code,m.state FROM logical_products p LEFT JOIN logical_code_migrations m ON m.product_id=p.id WHERE p.owner_id=? AND p.account_id=? AND (length(p.code)=8 OR m.product_id IS NOT NULL)",
            (owner, account),
        )
        unknown = self.store.rows(
            "SELECT count(*) AS n FROM logical_code_migration_items m JOIN outbox o ON o.id=m.operation_id JOIN logical_products p ON p.id=m.product_id WHERE p.owner_id=? AND p.account_id=? AND o.status='UNCERTAIN'",
            (owner, account),
        )[0]["n"]
        text = "🔄 <b>Перенос кодов товаров</b>\nНайдено: " + str(len(products))
        text += "\nПодтверждено: " + str(sum(p["state"] == "VERIFIED" for p in products))
        text += "\nРучная проверка: " + str(
            sum(p["state"] in {"BLOCKED", "MANUAL_REVIEW_REQUIRED"} for p in products)
        )
        text += "\nНеизвестный результат: " + str(unknown)
        text += "\nНазвание получит [XXX]. Настройки и история сохраняются. Миграция выполняется автоматически; неизвестные результаты сначала сверяются."
        return text, keyboard(
            [
                [("🔄 Выполнить безопасный перенос", "codes:prepare")],
                [("📋 Результаты и безопасная сверка", "codes:results:0")],
            ],
            back=("Инструменты", "tools:data"),
        )

    def results(self, owner, page):
        account = self.service.logical.authority(owner)
        rows = self.store.rows(
            "SELECT m.*,o.status AS external_status FROM logical_code_migration_items m JOIN logical_products p ON p.id=m.product_id LEFT JOIN outbox o ON o.id=m.operation_id WHERE p.owner_id=? AND p.account_id=? ORDER BY m.updated_at DESC,m.item_id LIMIT 11 OFFSET ?",
            (owner, account, page * 10),
        )
        text = "📋 <b>Результаты переноса</b>"
        buttons = []
        report = self.store.setting(f"title_codes:report:{account}:{owner}", {})
        problems = report.get("problems", [])
        if problems and page == 0:
            text += "\n\n⚠️ <b>Проблемы переноса</b>"
            for problem in problems[:6]:
                reason = {
                    "RATE_LIMITED": "Playerok ограничил запросы. Дождитесь снятия ограничения.",
                    "NETWORK_ERROR": "Не удалось прочитать Playerok. Изменение не подтверждено.",
                    "TimeoutError": "Проверка не завершилась. Требуется сверка результата.",
                }.get(problem["reason"], problem["reason"])
                name = "Каталог Playerok"
                links = self.store.rows(
                    "SELECT l.item_id,l.title_snapshot,p.baseline FROM logical_listings l JOIN logical_products p ON p.id=l.product_id WHERE l.product_id=? LIMIT 1",
                    (problem.get("product_id"),),
                )
                if links:
                    name = (
                        links[0]["title_snapshot"] or json.loads(links[0]["baseline"]).get("name") or "Товар"
                    )
                    buttons.append([("Открыть · " + name[:22], "item:" + links[0]["item_id"])])
                text += "\n• " + escaped(name, 70) + ": " + escaped(reason, 240)
        for row in rows[:10]:
            name = json.loads(row["after_snapshot"])["name"]
            state = row["external_status"] or row["state"]
            text += "\n\n" + escaped(name, 85) + "\n" + LABELS.get(state, "Нужна проверка")
            if state == "UNCERTAIN":
                buttons.append([("🔎 Сверить " + name[:22], "codes:reconcile:" + row["item_id"])])
            if row["operation_id"]:
                buttons.append([("Подробнее · " + name[:22], "result:show:" + row["operation_id"])])
        if not rows and not problems:
            text += "\nЗаписей пока нет. Запустите безопасный перенос из предыдущего экрана."
        if page:
            buttons.append([("←", f"codes:results:{page - 1}")])
        if len(rows) > 10:
            buttons.append([("→", f"codes:results:{page + 1}")])
        return text, keyboard(
            buttons + [[("🔄 Обновить", f"codes:results:{page}")]], back=("К переносу", "codes:home")
        )

    async def callback(self, action, owner, state):
        if action == "codes:home":
            await replace_context(state, None, {})
            return self.home(owner)
        if action == "codes:prepare":
            await self.service.run(owner)
            await replace_context(state, None, {})
            return self.results(owner, 0)
        if action.startswith("codes:results:"):
            return self.results(owner, max(0, min(100000, int(action.split(":")[2]))))
        if action.startswith("codes:reconcile:"):
            await self.service.reconcile(owner, action.split(":", 2)[2])
            return self.results(owner, 0)
        raise Conflict("Старая кнопка переноса устарела. Откройте результаты переноса")
