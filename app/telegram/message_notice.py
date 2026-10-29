from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard


def message_notice(payload, chat_id, note=""):
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
        keyboard(
            [
                [("✉️ Ответить", "reply:" + chat_id), ("📖 Диалог", "chat:" + chat_id)],
                [("✅ Прочитано", "readchat:" + chat_id)],
            ]
        )
        if chat_id
        else keyboard([])
    )
    return text, markup
