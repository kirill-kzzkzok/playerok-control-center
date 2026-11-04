from app.database.store import Conflict
from app.services.notification_preferences import LABELS, NotificationPreferences
from app.services.product_readiness import fresh
from app.telegram.keyboards import keyboard
from app.telegram.presentation import connection_summary


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
                "⚙️ <b>Настройки</b>\nВыберите, что настроить.",
                keyboard(
                    [
                        [("🔌 Подключения", "sp:connections")],
                        [("💳 Вывод средств", "providers")],
                        [("⭐ Лимиты Stars", "gft:limits")],
                        [("🔔 Уведомления", "sp:notifications")],
                        [("💬 Глобальные ответы на фразы", "ar:menu")],
                        [("Диагностика и обслуживание", "tools")],
                    ],
                    back=("⬅️ Мой магазин", "home"),
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
            telegram = self.store.setting("gifts:connection", {})
            telegram_status = (
                "✅ Подключён"
                if telegram.get("status") == "OK" and fresh(telegram.get("at"))
                else "Ожидает актуальной проверки"
            )
            return (
                "🔐 <b>Подключения</b>\n"
                + connection_summary(self.api)
                + "\nЛичный Telegram: "
                + telegram_status
                + "\nПоказаны сохранённые результаты. Управление подключениями — ниже.",
                keyboard(
                    [[("Playerok", "profile"), ("Личный Telegram", "gft:settings")]],
                    back=("⬅️ Настройки", "settings"),
                ),
            )
        if action == "sp:security":
            owners = self.store.rows("SELECT count(*) AS n FROM administrators")[0]["n"]
            return (
                f"🛡️ <b>Безопасность</b>\nЗакреплённых владельцев: {owners}\nВаш Telegram ID: <code>{owner}</code>"
                + "\nДоступ только в личном чате закреплённого владельца. Новый /start не заменяет владельца."
                + "\nСекреты и резервные копии хранятся локально. База содержит сообщения и данные выдачи."
                + "\nДенежные действия требуют отдельных разрешений. Настройка уведомлений не включает операции магазина."
                + "\nНеопределённые операции не повторяются автоматически; проверяйте их на Playerok.",
                keyboard(
                    [[("Неопределённые операции", "ops:list:UNCERTAIN:0")], [("Настройки", "settings")]]
                ),
            )
        if action == "sp:fulfillment":
            return (
                (
                    "✅ <b>Подтверждение выполнения</b>\nВ карточке заказа выберите «Подтвердить выполнение» только после передачи товара."
                    "\nПлощадка может потребовать отчёт. Получение подтверждает покупатель."
                    "\nДля подарков автоматическая отметка продавца требует отдельного разрешения и подтверждения всех подарков. Этот экран ничего не включает."
                ),
                keyboard([[("Заказы", "orders"), ("Автовыдача", "automation")], [("Настройки", "settings")]]),
            )
        raise ValueError("Раздел настроек недоступен")
