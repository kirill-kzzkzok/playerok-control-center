from app.database.store import Conflict
from app.services.notification_preferences import LABELS, NotificationPreferences
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard


class SettingsPanel:
    def __init__(self, store, api):
        self.store, self.api = store, api
        self.preferences = NotificationPreferences(store)

    def owner(self, actor):
        if not self.store.rows("SELECT 1 FROM administrators WHERE user_id=?", (actor,)):
            raise Conflict("Доступ закрыт")

    def notifications(self, owner):
        self.owner(owner)
        revision, enabled = self.preferences.get(owner)
        rows = [
            [
                (
                    ("🔔 " if enabled[key] else "🔕 ") + label,
                    f"sp:notify:{revision}:{key}:{int(not enabled[key])}",
                )
            ]
            for key, label in LABELS.items()
        ]
        rows.append([("Настройки", "settings")])
        return (
            (
                "🔔 <b>Уведомления</b>\nНажмите тип события, чтобы изменить его настройку."
                "\nНастройки сохраняются для вашего Telegram ID. Отключённые события пропускаются без отправки и не пересылаются после включения."
                "\n\nПредупреждения о неопределённом результате операций обязательны: повтор может привести к двойной отправке."
                "\nУведомления не управляют самой автовыдачей или продвижением. Уже отправленные сообщения и обновления их статусов сохраняются."
            ),
            keyboard(rows),
        )

    def handle(self, action, owner):
        self.owner(owner)
        if action == "settings":
            return (
                "⚙️ <b>Настройки</b>\nЗаписи API: "
                + ("включены" if self.api.account.writes_enabled else "выключены")
                + "\nПлатежи и автовывод запрещены. Владелец закреплён локально.",
                keyboard(
                    [
                        [("🔐 Подключения", "sp:connections"), ("🛡️ Безопасность", "sp:security")],
                        [("⚡ Автовыдача", "automation"), ("⬆️ Автоподнятие · настройки", "promotions")],
                        [("✅ Подтверждение выполнения", "sp:fulfillment")],
                        [("💬 Ответы и шаблоны", "quickreplies"), ("🔔 Уведомления", "sp:notifications")],
                        [("💬 Правила автоответов", "ar:menu")],
                        [("💰 Финансовые параметры", "providers"), ("📊 Статистика", "stats:7")],
                        [("💾 Резервная копия БД", "backup"), ("🩺 Диагностика", "diagnostics")],
                        [("🧹 Очистка интерфейса", "ic:preview")],
                    ]
                ),
            )
        if action == "sp:notifications":
            return self.notifications(owner)
        if action.startswith("sp:notify:"):
            _, _, revision, key, enabled = action.split(":")
            if enabled not in {"0", "1"}:
                raise ValueError("Недопустимое значение")
            self.preferences.set(owner, int(revision), key, enabled == "1")
            return self.notifications(owner)
        if action == "sp:connections":
            return (
                "🔐 <b>Подключения</b>\nPlayerok: "
                + escaped(self.store.setting("playerok:connection", "ещё не проверен"), 100)
                + "\nСверка заказов: "
                + escaped(self.store.setting("sync:health", {}).get("status", "NOT_STARTED"), 100)
                + "\nТранзакции: "
                + escaped(self.store.setting("transactions:health", {}).get("status", "NOT_STARTED"), 100)
                + "\n\nЭто сохранённые состояния, не новая проверка сети. Токены и cookies меняются только в локальном .env и в Telegram не показываются."
                + "\nДля read-only проверки HTTP: .venv/Scripts/python -m app.auth_diagnostics",
                keyboard([[("Подробная диагностика", "diagnostics"), ("Настройки", "settings")]]),
            )
        if action == "sp:security":
            owners = self.store.rows("SELECT count(*) AS n FROM administrators")[0]["n"]
            return (
                f"🛡️ <b>Безопасность</b>\nЗакреплённых владельцев: {owners}\nВаш Telegram ID: <code>{owner}</code>"
                + "\nДоступ только в личном чате закреплённого владельца. Новый /start не заменяет владельца."
                + "\nСекреты и резервные копии хранятся локально. База содержит сообщения и данные выдачи."
                + "\nПлатные операции и автоматический вывод заблокированы. Переключение настроек уведомлений не включает записи API."
                + "\nНеопределённые операции не повторяются автоматически; проверяйте их на Playerok.",
                keyboard(
                    [[("Неопределённые операции", "ops:list:UNCERTAIN:0")], [("Настройки", "settings")]]
                ),
            )
        if action == "sp:fulfillment":
            return (
                (
                    "✅ <b>Подтверждение выполнения</b>\nВ карточке оплаченного заказа выберите «Я выполнил заказ». "
                    "Требуются проверка передачи товара и подтверждение владельца; для официального продавца проверяется требование отчёта."
                    "\nФлаг mark_sent у цепочки завершает её состоянием NEEDS_FULFILLMENT, а не автоматической записью SENT."
                    "\nПодтверждение получения покупателем бот не подделывает. Полностью автоматический SENT без личной проверки не реализован."
                ),
                keyboard([[("Заказы", "orders"), ("Автовыдача", "automation")], [("Настройки", "settings")]]),
            )
        raise ValueError("Раздел настроек недоступен")
