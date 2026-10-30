from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def keyboard(rows, home=True):
    buttons = [
        [InlineKeyboardButton(text=text, callback_data=callback) for text, callback in row] for row in rows
    ]
    if home:
        buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def main_menu():
    return keyboard(
        [
            [("👤 Профиль", "profile"), ("📊 Статистика", "stats:7")],
            [("📦 Мои товары", "items"), ("➕ Создать товар", "create")],
            [("🛒 Заказы", "orders"), ("💬 Сообщения", "chats")],
            [("⚡ Автоматизация", "automation"), ("⚙️ Настройки", "settings")],
            [("💾 Черновики", "drafts"), ("🩺 Диагностика", "diagnostics")],
            [("🧾 Результаты операций", "result:list:0"), ("📦 Выдано — подтвердить", "delivered:0")],
        ],
        home=False,
    )
