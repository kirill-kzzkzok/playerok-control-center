from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def keyboard(rows, home=True, *, back=None):
    buttons = [
        [InlineKeyboardButton(text=text, callback_data=callback) for text, callback in row] for row in rows
    ]
    if back:
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back[1])])
    elif home:
        buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def main_menu(*, attention=False):
    return keyboard(
        [
            [("📦 Товары", "items"), ("🛒 Заказы", "orders")],
            [("💬 Сообщения", "chats"), ("🚀 Продвижение", "promotions")],
            [("📊 Статистика", "stats:7"), ("⚙️ Настройки", "settings")],
        ]
        + (
            [[(f"⚠️ Важные заказы: {attention}", "attention")]]
            if type(attention) is int and attention > 0
            else []
        ),
        home=False,
    )
