"""Small local navigation hub; no synchronisation or external writes on open."""

import json
import time

from app.database.store import Conflict
from app.gifts.outcomes import DELAYED_PREFLIGHT_SQL
from app.services.fulfillment import ACTIVE_UNKNOWN_SQL
from app.services.order_management import ENROLLMENT_WAIT_SQL
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard, main_menu
from app.telegram.presentation import connection_summary, enrollment_wait_text
from app.workers.supervisor import CYCLE_LABELS


class HomePanel:
    def __init__(self, store, api):
        self.store, self.api = store, api

    def paid_issues(self, page=0):
        if not 0 <= page <= 1_000_000:
            raise Conflict("Страница недоступна")
        return self.store.rows(
            "WITH waiting AS ("
            + ENROLLMENT_WAIT_SQL
            + "), issues AS (SELECT o.id,o.status,json_extract(o.payload,'$.item.name') AS name, "
            "COALESCE(w.wait_seconds>=180,0) AS enrollment_wait,w.wait_seconds,w.error_code,w.due_at, "
            "EXISTS(SELECT 1 FROM auto_enrollment e WHERE e.order_id=o.id AND e.status='BLOCKED') AS enrollment, "
            "(SELECT c.status FROM delivery_chains c WHERE c.order_id=o.id) AS chain, "
            "EXISTS(SELECT 1 FROM reservations r JOIN inventory i ON i.id=r.inventory_id "
            "WHERE r.order_id=o.id AND i.status='QUARANTINED') AS quarantine, "
            "EXISTS(SELECT 1 FROM outbox q WHERE q.target=o.id AND (q.status='UNCERTAIN' "
            "OR q.status='PROCESSING' AND q.lease_until<=?) AND " + ACTIVE_UNKNOWN_SQL + ") AS uncertain, "
            "EXISTS(SELECT 1 FROM gift_jobs g WHERE g.order_id=o.id AND "
            "(g.state='UNCERTAIN' OR g.refund_state='UNCERTAIN')) AS payment_unknown, "
            "EXISTS(SELECT 1 FROM gift_jobs g WHERE g.order_id=o.id "
            "AND g.state IN ('BLOCKED','INVALID_RECIPIENT')) AS gift_blocked, "
            "(EXISTS(SELECT 1 FROM gift_listings g "
            "LEFT JOIN logical_listings l ON l.item_id=g.item_id "
            "LEFT JOIN logical_products p ON p.id=l.product_id "
            "WHERE (g.item_id=o.item_id OR EXISTS(SELECT 1 FROM logical_order_scopes s "
            "WHERE s.order_id=o.id AND s.product_id=p.id)) "
            "AND (g.enabled=0 OR p.enabled=0 OR EXISTS(SELECT 1 FROM settings st "
            "WHERE st.key='delivery:'||g.item_id||':enabled' AND st.value='false')) "
            "AND NOT EXISTS(SELECT 1 FROM gift_jobs j WHERE j.order_id=o.id AND j.state='SUCCEEDED')) "
            "OR EXISTS(SELECT 1 FROM delivery_chains c WHERE c.order_id=o.id AND c.status='CANCELLED' "
            "AND NOT EXISTS(SELECT 1 FROM delivery_chain_steps s JOIN outbox b ON b.id=s.operation_id "
            "WHERE s.order_id=o.id AND s.kind IN ('delivery','delivery_text') AND b.status='SUCCEEDED'))) "
            "AS stopped_delivery, "
            "EXISTS(SELECT 1 FROM (" + DELAYED_PREFLIGHT_SQL + ") q WHERE q.target=o.id) AS delayed, "
            "EXISTS(SELECT 1 FROM gift_listings g JOIN logical_listings l ON l.item_id=g.item_id "
            "JOIN logical_products p ON p.id=l.product_id WHERE g.item_id=o.item_id "
            "AND g.enabled=1 AND p.state='LEGACY' "
            "AND NOT EXISTS(SELECT 1 FROM gift_jobs j WHERE j.order_id=o.id)) AS binding_unverified FROM orders o LEFT JOIN waiting w ON w.order_id=o.id), "
            "actionable AS (SELECT * FROM issues WHERE uncertain OR payment_unknown OR "
            "status IN ('PAID','SENT') AND (enrollment OR chain IN ('BLOCKED','NEEDS_FULFILLMENT') "
            "OR quarantine OR gift_blocked OR binding_unverified OR delayed OR stopped_delivery OR enrollment_wait)) "
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
        if row.get("enrollment_wait") and not (
            reasons
            or row.get("stopped_delivery")
            or row["enrollment"]
            or row["chain"] in {"BLOCKED", "NEEDS_FULFILLMENT"}
            or row["gift_blocked"]
            or row["binding_unverified"]
        ):
            reasons.append(enrollment_wait_text(row))
        if row.get("delayed"):
            reasons.append("Выдача долго ожидает соединения. Безопасные проверки продолжаются автоматически")
        if row.get("stopped_delivery"):
            reasons.append("Выдача товара выключена, но оплаченный заказ ещё не выдан")
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
                "SELECT 1 FROM outbox q LEFT JOIN orders o ON o.id=q.target "
                "WHERE q.status='UNCERTAIN' AND " + ACTIVE_UNKNOWN_SQL + " LIMIT 1",
            ),
            (
                "Действия не выполнены",
                "ops:list:ALL:0",
                (
                    "SELECT 1 FROM outbox WHERE status='PERMANENT_FAILED' AND COALESCE(error_code,'') NOT IN ('MANUALLY_STOPPED','OWNER_STOPPED') "
                    "AND NOT (COALESCE(error_code,'')='MANUALLY_STOPPED_UNCERTAIN' AND kind='edit' "
                    "AND COALESCE(json_extract(payload,'$.logical_binding'),0)=0 "
                    "AND COALESCE(json_extract(payload,'$.code_migration'),0)=0) LIMIT 1"
                ),
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

    def overview(self, owner, *, paid_problems=0, rechecking=False):
        from app.services.product_readiness import fresh
        from app.services.statistics import parsed_time
        from app.telegram.presentation import local_time

        account = getattr(self.api.account, "id", None)
        cache = self.store.setting("ui:catalog:" + str(account), {})
        observed = self.store.setting("shop:active:" + str(account), {})
        observations = []
        if fresh(observed.get("at")):
            observations.append((parsed_time(observed["at"]), observed.get("count")))
        if cache.get("complete") and fresh(cache.get("at")):
            observations.append(
                (
                    parsed_time(cache["at"]),
                    sum(item.get("status") == "APPROVED" for item in cache.get("items", [])),
                )
            )
        complete = self.store.setting("logical:scan:" + str(account) + ":completed", {})
        if complete.get("generation") and fresh(complete.get("at")):
            count = self.store.rows(
                "SELECT COUNT(*) AS n FROM logical_scan_items WHERE account_id=? AND generation=? "
                "AND json_extract(payload,'$.status')='APPROVED' "
                "AND json_extract(payload,'$.deleted_at') IS NULL",
                (account, complete["generation"]),
            )[0]["n"]
            observations.append((parsed_time(complete["at"]), count))
        active = max(observations, key=lambda observation: observation[0])[1] if observations else None
        paid = self.store.rows(
            "SELECT COUNT(*) AS n FROM orders WHERE status IN ('PAID','SENT') "
            "AND json_extract(payload,'$.direction')='OUT' "
            "AND json_extract(payload,'$.item.user.id')=?",
            (account,),
        )[0]["n"]
        text = "\n"
        if active is not None or not rechecking:
            text += "\n📦 Активные товары: " + (str(active) if active is not None else "обновляем данные")
        text += f"\n🛒 Оплаченные заказы: {paid}"
        funds = self.store.setting("playerok:balance:" + str(account), {})
        from app.services.finance import money

        def amount(key):
            try:
                return f"{money(funds.get(key)):,.2f}".replace(",", " ").removesuffix(".00") + " ₽"
            except (ValueError, TypeError):
                return "нет данных"

        text += f"\n💰 Баланс Playerok: {amount('value')}"
        text += f"\nДоступно: {amount('available')}\n🔒 Заморожено: {amount('frozen')}"
        if funds.get("at"):
            text += (
                "\nДанные "
                + ("обновлены " if fresh(funds["at"], 300) else "устарели · ")
                + local_time(funds["at"])
            )
        if paid_problems:
            text += f" · требуют действия: {paid_problems}"
        enabled = self.store.rows(
            "SELECT COUNT(*) AS n FROM logical_products WHERE account_id=? AND enabled=1", (account,)
        )[0]["n"]
        text += f"\n⚡ Автовыдача включена у товаров: {enabled}"
        connection = getattr(self.api, "connection", None)
        writes = getattr(connection, "desired_writes", getattr(self.api.account, "writes_enabled", False))
        automation = getattr(
            connection, "desired_automation", getattr(self.api.account, "automation_enabled", False)
        )
        if enabled and not (writes and automation):
            text += "\n⏸ Запуск автоматики запрещён настройками подключения"
        has_gifts = self.store.rows("SELECT 1 FROM gift_listings LIMIT 1")
        if has_gifts and self.store.setting("gifts:stopped", True):
            text += "\n⛔ Подарочные покупки остановлены владельцем"
        elif has_gifts and self.store.setting("gifts:paused", False):
            text += "\n⏸ Подарочные покупки временно приостановлены"
        balance = self.store.setting("gifts:balance", {})
        if type(balance.get("stars")) is int and fresh(balance.get("at")):
            text += f"\n⭐ Баланс: {balance['stars']} ⭐"
        else:
            text += "\n⭐ Баланс: обновляем данные"
        profit = self.store.setting(f"shop:profit:{account}:{owner}", {}) if owner else {}
        if not fresh(profit.get("at"), 86400) or "net" not in profit:
            text += "\n💰 Прибыль: ожидаем подтверждённые данные"
        else:
            text += "\n\n💰 <b>За последние 24 часа</b>"
            if "awaiting_confirmation" in profit:
                text += f"\nЖдут подтверждения: {profit['awaiting_confirmation']}"
            for label, key in (
                ("Зачислено", "sales_credit"),
                ("Себестоимость", "cost"),
                ("Поднятия", "premium_bump"),
                ("Размещение PREMIUM", "premium_publish"),
                ("PREMIUM без категории", "premium_unclassified"),
                ("Другие размещения", "other_placement"),
                ("Комиссия вывода", "withdrawal"),
                ("Прочие расходы", "other"),
                ("Итог по учтённым суммам", "net"),
            ):
                if key in profit and (key in {"sales_credit", "net"} or profit[key] != "0.00"):
                    text += f"\n{label}: {escaped(profit[key], 30)} ₽"
            if "premium_bump" not in profit and profit.get("promotion") not in {None, "0.00"}:
                text += f"\nПродвижение: {escaped(profit['promotion'], 30)} ₽"
            if not fresh(profit["at"]):
                text += "\nСохранённый расчёт на " + local_time(profit["at"])
        return text

    def home(self, owner=None):
        paid = self.paid_issues()
        paid_total = paid[0]["total"] if paid else 0
        connection = connection_summary(self.api)
        status = getattr(getattr(self.api, "connection", None), "state", {}).get("status")
        note = ""
        if status == "DISCONNECTED":
            note = "\nПроверьте подключение в профиле."
        worker_problem = next(
            (row[0][0] for row in self.problems(include_paid=False) if row[0][1] == "diagnostics:workers"),
            None,
        )
        if worker_problem:
            note += "\n\n" + escaped(worker_problem)
        return (
            "🏠 <b>Мой магазин</b>\n\n"
            + connection
            + self.overview(owner, paid_problems=paid_total, rechecking=status == "UNKNOWN")
            + note,
            main_menu(attention=paid_total),
        )

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
            text = "⚠️ <b>Оплаченные заказы: важное</b>\n"
            if paid:
                text += f"Проблемных заказов: {total}\n"
                text += (
                    "\n\n".join(
                        escaped(str(row["name"] or "Товар"), 90) + "\n" + escaped(self.paid_reason(row), 600)
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
                "Бот продолжает безопасные проверки автоматически. Для конфликтов причина указана в заказе. Повтор отправки не запускается."
                if rows
                else "В сохранённых данных проблем нет."
            ), keyboard(rows, back=("⬅️ Мой магазин", "home"))
        if action == "more":
            from app.telegram.settings_panel import SettingsPanel

            return SettingsPanel(self.store, self.api).handle("settings", owner)
        groups = {
            "tools": (
                "🛠 Инструменты",
                [
                    [("История и результаты", "tools:operations")],
                    [("История выдачи", "tools:delivery")],
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
                "📦 История выдачи",
                [
                    [("Доставленные заказы", "delivered:0")],
                    [("Ход выполнения заказов", "aq:list:chains:0")],
                    [("Обработка оплаченных заказов", "aq:list:enrollment:0")],
                    [("Настройки товаров", "aq:list:policies:0")],
                ],
            ),
            "tools:data": (
                "🛠 Диагностика и данные",
                [
                    [("Безопасность", "sp:security")],
                    [("Подтверждение выполнения", "sp:fulfillment")],
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
            rows, back=("⬅️ Настройки", "settings") if action == "tools" else ("⬅️ Инструменты", "tools")
        )
