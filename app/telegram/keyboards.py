from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def keyboard(rows, home=True, *, back=None):
    buttons = [
        [InlineKeyboardButton(text=text, callback_data=callback) for text, callback in row] for row in rows
    ]
    if back:
        buttons.append([InlineKeyboardButton(text=back[0], callback_data=back[1])])
    elif home:
        buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def main_menu(*, attention=False):
    return keyboard(
        [
            [("📦 Товары", "items"), ("🛒 Заказы", "orders")],
            [("💬 Сообщения", "chats"), ("🎁 Подарки", "gft:home")],
            [("⚙️ Ещё", "more")],
        ]
        + ([[("⚠️ Требует внимания", "attention")]] if attention else []),
        home=False,
    )
