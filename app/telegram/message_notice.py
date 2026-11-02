from app.services.chat_events import system_event
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard


def message_notice(payload, chat_id, note=""):
    event = system_event(payload)
    if event:
        return "🔔 <b>Событие Playerok</b>\n" + escaped(
            event, 300
        ) + "\nОткройте диалог и проверьте актуальную сделку.", keyboard(
            [[("💬 Открыть переписку", "chat:" + chat_id)]] if chat_id else []
        )
    user = payload.get("user") or {}
    author = escaped(user.get("username") or "Неизвестный отправитель", 120)
    text = (
        "💬 <b>Новое сообщение</b>\n👤 "
        + author
        + "\n"
        + escaped(
            "[сообщение удалено]"
            if payload.get("deleted_at")
            else payload.get("text") or "[вложение/событие]",
            2600,
        )
    )
    if payload.get("images"):
        text += f"\n📷 Изображений: {len(payload['images'])}"
    if note:
        text += "\n\n" + note
    markup = (
        keyboard([[("✉️ Ответить", "compose:" + chat_id), ("✖️ Закрыть", "chatclose:" + chat_id)]], home=False)
        if chat_id
        else keyboard([], home=False)
    )
    return text, markup
