import secrets

from app.database.store import Conflict
from app.services.interface_cleanup import InterfaceCleanup
from app.telegram.keyboards import keyboard


class InterfaceCleanupPanel:
    def __init__(self, store):
        self.service = InterfaceCleanup(store)

    async def callback(self, action, owner, state, message):
        if message.chat.id != owner or message.chat.type != "private":
            raise Conflict("Очистка доступна только в личном чате владельца")
        active_id = (
            self.service.store.setting(f"navigation:{message.bot.id}:{owner}", {}).get("id")
            or message.message_id
        )
        if action == "ic:preview":
            rows = self.service.preview(owner, message.bot.id, active_id)
            totals = self.service.totals(owner, message.bot.id)
            nonce = secrets.token_hex(8)
            await state.set_state("interface_cleanup:confirm")
            await state.set_data(
                {"interface_cleanup": {"nonce": nonce, "rows": rows, "bot_id": message.bot.id}}
            )
            text = (
                f"🧹 <b>Очистка интерфейса</b>\nК удалению: {len(rows)} сообщений панели (до 20 за раз)."
                f"\nЖурнал: удалено {totals.get('DELETED', 0)}, отклонено {totals.get('FAILED', 0)}, неизвестно {totals.get('UNCERTAIN', 0)}."
                "\nТолько сообщения бота, записанные новой системой учёта, без изменений последние 5 минут и не старше 47 часов."
                "\nТекущая карточка, уведомления и ваши сообщения сохраняются. Старые сообщения до включения учёта не угадываются по ID."
                "\n\nУдаление в Telegram необратимо. История Playerok, заказы, запас, черновики и локальная БД не очищаются."
            )
            buttons = [[("Удалить выбранные сообщения", "ic:commit:" + nonce)]] if rows else []
            return text, keyboard(buttons + [[("Отмена", "settings")]])
        if action.startswith("ic:commit:"):
            data = (await state.get_data()).get("interface_cleanup")
            if (
                not data
                or await state.get_state() != "interface_cleanup:confirm"
                or action.split(":", 2)[2] != data.get("nonce")
                or data.get("bot_id") != message.bot.id
            ):
                raise Conflict("Подтверждение очистки устарело")
            result = await self.service.execute(owner, message.bot, active_id, data["rows"], data["nonce"])
            await state.clear()
            return (
                f"🧹 Удалено: {result.get('DELETED', 0)} · отклонено: {result.get('FAILED', 0)} · неизвестный результат: {result.get('UNCERTAIN', 0)}"
                + "\nАвтоматического повтора нет. Сохранённые результаты Playerok и данные выдачи не менялись.",
                keyboard([[("Новое превью очистки", "ic:preview"), ("Настройки", "settings")]]),
            )
        raise ValueError("Действие очистки недоступно")
