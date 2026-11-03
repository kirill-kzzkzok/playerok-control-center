"""Small local navigation hub; no synchronisation or external writes on open."""

import json
import time

from app.database.store import Conflict
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard, main_menu
from app.telegram.presentation import connection_summary
from app.workers.supervisor import CYCLE_LABELS


class HomePanel:
    def __init__(self, store, api):
        self.store, self.api = store, api

    def paid_issues(self, page=0):
        if not 0 <= page <= 1_000_000:
            raise Conflict("Страница недоступна")
        return self.store.rows(
            "WITH issues AS (SELECT o.id,o.status,json_extract(o.payload,'$.item.name') AS name, "
            "EXISTS(SELECT 1 FROM auto_enrollment e WHERE e.order_id=o.id AND e.status='BLOCKED') AS enrollment, "
            "(SELECT c.status FROM delivery_chains c WHERE c.order_id=o.id) AS chain, "
            "EXISTS(SELECT 1 FROM reservations r JOIN inventory i ON i.id=r.inventory_id "
            "WHERE r.order_id=o.id AND i.status='QUARANTINED') AS quarantine, "
            "EXISTS(SELECT 1 FROM outbox q WHERE q.target=o.id AND (q.status='UNCERTAIN' "
            "OR q.status='PROCESSING' AND q.lease_until<=?)) AS uncertain, "
            "EXISTS(SELECT 1 FROM gift_jobs g WHERE g.order_id=o.id AND "
            "(g.state='UNCERTAIN' OR g.refund_state='UNCERTAIN')) AS payment_unknown, "
            "EXISTS(SELECT 1 FROM gift_jobs g WHERE g.order_id=o.id "
            "AND g.state IN ('BLOCKED','INVALID_RECIPIENT')) AS gift_blocked, "
            "EXISTS(SELECT 1 FROM gift_listings g JOIN logical_listings l ON l.item_id=g.item_id "
            "JOIN logical_products p ON p.id=l.product_id WHERE g.item_id=o.item_id "
            "AND g.enabled=1 AND p.state='LEGACY' "
            "AND NOT EXISTS(SELECT 1 FROM gift_jobs j WHERE j.order_id=o.id)) AS binding_unverified FROM orders o), "
            "actionable AS (SELECT * FROM issues WHERE uncertain OR payment_unknown OR "
            "status IN ('PAID','SENT') AND (enrollment OR chain IN ('BLOCKED','NEEDS_FULFILLMENT') "
            "OR quarantine OR gift_blocked OR binding_unverified)) "
            "SELECT *,COUNT(*) OVER() AS total FROM actionable "
            "ORDER BY (uncertain OR payment_unknown OR quarantine) DESC,id LIMIT 10 OFFSET ?",
            (time.time(), page * 10),
        )

    @staticmethod
    def paid_reason(row):
        reasons = []
        if row["uncertain"] or row["payment_unknown"]:
            reasons.append("Результат операции неизвестен. Сверьте без повторной отправки")
        if row["quarantine"]:
            reasons.append("Выдача запаса требует проверки; данные остаются в карантине")
        if row["enrollment"] or row["chain"] == "BLOCKED":
            reasons.append("Оплаченная выдача заблокирована. Проверьте причину в заказе")
        if row["chain"] == "NEEDS_FULFILLMENT":
            reasons.append("Нужно проверить завершение сделки на Playerok")
        if row["gift_blocked"]:
            reasons.append("Подарок не выдан. Проверьте получателя и разрешения")
        if row["binding_unverified"]:
            reasons.append("Старая подарочная привязка требует подтверждения [XXX]. Заказ проверьте вручную")
        return "; ".join(reasons)

    def problems(self, *, include_paid=True):
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
        paid = self.paid_issues() if include_paid else []
        if paid:
            rows.insert(0, [(f"⛔ Оплаченные заказы требуют действия: {paid[0]['total']}", "attention:0")])
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
        for worker in self.store.rows("SELECT key,value FROM settings WHERE key GLOB 'runtime:cycle:*'"):
            health = json.loads(worker["value"])
            stalled = health.get("state") == "RUNNING" and 0 < health.get("deadline_at", 0) < time.time()
            if (
                stalled
                or health.get("consecutive_failures", 0) >= 3
                and health.get("state") in {"ERROR", "DEGRADED"}
            ):
                label = CYCLE_LABELS.get(worker["key"].rsplit(":", 1)[1], "Фоновая обработка")
                rows.append([("⚠️ " + label + " требует проверки", "diagnostics:workers")])
        return rows

    def home(self):
        problems = self.problems()
        connection = connection_summary(self.api)
        note = (
            "Есть вопросы, требующие решения."
            if problems
            else (
                "В локальной истории проблем нет."
                if getattr(getattr(self.api, "connection", None), "state", {}).get("status") == "CONNECTED"
                and getattr(self.api.account, "connection_verified", False)
                else "Проверьте подключение в профиле."
            )
        )
        return "🏠 <b>Мой магазин</b>\n\n" + connection + "\n" + note, main_menu(attention=bool(problems))

    def show(self, action, owner):
        if not self.store.rows("SELECT 1 FROM administrators WHERE user_id=?", (owner,)):
            raise Conflict("Доступ закрыт")
        if action == "attention" or action.startswith("attention:"):
            page = int(action.split(":", 1)[1]) if ":" in action else 0
            paid = self.paid_issues(page)
            rows = [
                [(str(row["name"] or "Товар")[:40] + " · проверить заказ", "order:" + row["id"])]
                for row in paid
            ]
            total = paid[0]["total"] if paid else 0
            text = "⚠️ <b>Требует моего действия</b>\n"
            if paid:
                text += f"Проблемных заказов: {total}\n"
                text += (
                    "\n\n".join(
                        escaped(str(row["name"] or "Товар"), 90) + "\n" + escaped(self.paid_reason(row), 270)
                        for row in paid
                    )
                    + "\n"
                )
            navigation = []
            if page:
                navigation.append(("← Назад", f"attention:{page - 1}"))
            if total > (page + 1) * 10:
                navigation.append(("Далее →", f"attention:{page + 1}"))
            if navigation:
                rows.append(navigation)
            rows += self.problems(include_paid=False)
            return text + (
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
