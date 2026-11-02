"""Small local navigation hub; no synchronisation or external writes on open."""

from app.database.store import Conflict
from app.telegram.keyboards import keyboard, main_menu
from app.telegram.presentation import connection_summary


class HomePanel:
    def __init__(self, store, api):
        self.store, self.api = store, api

    def problems(self):
        checks = [
            (
                "Операции с неизвестным результатом",
                "ops:list:UNCERTAIN:0",
                "SELECT 1 FROM outbox WHERE status='UNCERTAIN' LIMIT 1",
            ),
            (
                "Действия не выполнены",
                "ops:list:ALL:0",
                "SELECT 1 FROM outbox WHERE status='PERMANENT_FAILED' AND COALESCE(error_code,'') NOT IN ('MANUALLY_STOPPED','OWNER_STOPPED') LIMIT 1",
            ),
            (
                "Подарки требуют проверки",
                "gft:list:uncertain:0",
                "SELECT 1 FROM gift_jobs WHERE state='UNCERTAIN' OR refund_state='UNCERTAIN' LIMIT 1",
            ),
            (
                "Подарочная выдача остановлена",
                "gft:list:failed:0",
                "SELECT 1 FROM gift_jobs WHERE state IN ('BLOCKED','INVALID_RECIPIENT') LIMIT 1",
            ),
            (
                "Заказы с проблемой",
                "orderpage:0:PROBLEM",
                "SELECT 1 FROM orders o WHERE json_extract(o.payload,'$.has_problem')=1 OR EXISTS(SELECT 1 FROM order_annotations a WHERE a.order_id=o.id AND a.problem=1) LIMIT 1",
            ),
        ]
        rows = [[(label, path)] for label, path, sql in checks if self.store.rows(sql)]
        if self.store.rows("SELECT 1 FROM gift_jobs WHERE refund_state IN ('MANUAL','FAILED') LIMIT 1"):
            rows.append([("Возврат требует решения", "gft:list:refund:0")])
        for row in self.store.rows(
            "SELECT p.code,l.item_id FROM logical_products p JOIN logical_listings l ON l.product_id=p.id WHERE p.error_code IS NOT NULL GROUP BY p.id LIMIT 10"
        ):
            rows.append([("Проверить товар " + row["code"], "lp:item:" + row["item_id"])])
        if self.store.setting("sync:health", {}).get("status") == "ERROR":
            rows.append([("Не удалось синхронизировать магазин", "diagnostics")])
        if getattr(getattr(self.api, "connection", None), "state", {}).get("status") == "DISCONNECTED":
            rows.append([("Playerok не подключён", "profile")])
        return rows

    def home(self):
        problems = self.problems()
        connection = connection_summary(self.api)
        note = (
            "Есть вопросы, требующие решения."
            if problems
            else (
                "В локальной истории проблем нет."
                if connection.startswith("🟢")
                else "Проверьте подключение в профиле."
            )
        )
        return "🏠 <b>Мой магазин</b>\n\n" + connection + "\n" + note, main_menu(attention=bool(problems))

    def show(self, action, owner):
        if not self.store.rows("SELECT 1 FROM administrators WHERE user_id=?", (owner,)):
            raise Conflict("Доступ закрыт")
        if action == "attention":
            rows = self.problems()
            return "⚠️ <b>Требует внимания</b>\n" + (
                "Выберите вопрос для проверки. Повтор отправки не запускается."
                if rows
                else "В сохранённых данных проблем нет."
            ), keyboard(rows, back=("⬅️ Мой магазин", "home"))
        if action == "more":
            return "⚙️ <b>Ещё</b>", keyboard(
                [
                    [("📊 Статистика", "stats:7"), ("⚡ Автовыдача", "automation")],
                    [("🚀 Автопродвижение", "promotions")],
                    [("👤 Профиль", "profile"), ("⚙️ Настройки", "settings")],
                    [("🛠 Инструменты", "tools")],
                ],
                back=("⬅️ Мой магазин", "home"),
            )
        groups = {
            "tools": (
                "🛠 Инструменты",
                [
                    [("История и результаты", "tools:operations")],
                    [("Выдача и цепочки", "tools:delivery")],
                    [("Диагностика и данные", "tools:data")],
                    [("История продвижения", "tools:promotion")],
                ],
            ),
            "tools:operations": (
                "🧾 История и результаты",
                [
                    [("История операций", "ops:list:ALL:0")],
                    [("Неопределённые операции", "ops:list:UNCERTAIN:0")],
                    [("Результаты действий", "result:list:0")],
                ],
            ),
            "tools:delivery": (
                "📦 Выдача и цепочки",
                [
                    [("Выдано — ожидает подтверждения", "delivered:0")],
                    [("Цепочки выдачи", "aq:list:chains:0")],
                    [("Автовыдача новых заказов", "aq:list:enrollment:0")],
                    [("Все настройки выдачи", "aq:list:policies:0")],
                ],
            ),
            "tools:data": (
                "🛠 Диагностика и данные",
                [
                    [("Диагностика", "diagnostics")],
                    [("Синхронизировать", "sync")],
                    [("Условия продавца", "agv:list:0")],
                    [("🔄 Перенос кодов товаров", "codes:home")],
                    [("Резервная копия", "backup")],
                    [("Очистка интерфейса", "ic:preview")],
                ],
            ),
            "tools:promotion": (
                "🚀 История проверок",
                [
                    [("Проверки", "aq:list:evaluations:0")],
                    [("Расходы", "aq:list:expenses:0")],
                    [("Выбранные товары", "aq:list:targets:0")],
                    [("Расписание", "aq:list:schedule:0")],
                ],
            ),
        }
        title, rows = groups[action]
        return title, keyboard(
            rows, back=("⬅️ Ещё", "more") if action == "tools" else ("⬅️ Инструменты", "tools")
        )
