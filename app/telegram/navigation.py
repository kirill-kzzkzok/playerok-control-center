"""One owner UI panel; durable rendering outcomes, separate from business receipts."""

import asyncio
import secrets

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InputMediaPhoto

from app.database.store import Conflict
from app.security.logging import event
from app.telegram.keyboards import keyboard


def safe_route(action):
    """Replayable view capabilities, never confirmations or monetary actions."""
    return action in {
        "home",
        "more",
        "tools",
        "attention",
        "items",
        "chats",
        "orders",
        "settings",
        "diagnostics",
        "profile",
        "profile:details",
        "providers",
        "drafts",
        "promotions",
        "automation",
        "gft:home",
        "gft:settings",
        "gft:bindings",
        "gft:catalog",
        "ar:menu",
        "ar:list",
    } or action.startswith(
        (
            "item:",
            "iv:",
            "order:",
            "chat:",
            "stats:",
            "sd:",
            "sr:",
            "st:",
            "finance:",
            "stock:view:",
            "gft:item:",
            "gft:job:",
            "gft:list:",
            "payp:item:",
            "auto:item:",
            "ops:list:",
            "ops:show:",
            "result:show:",
            "draft:",
            "tools:",
            "od:details:",
            "pf:card:",
            "aq:list:",
            "aq:chain:",
            "ar:show:",
        )
    )


CONTEXT = {
    "item_filters",
    "order_search",
    "order_filter",
    "list_cursor",
    "list_number",
    "list_kind",
    "gift_picker",
}


class Navigation:
    def __init__(self, store):
        self.store = store
        if not hasattr(store, "navigation_locks"):
            store.navigation_locks = {}

    def key(self, bot, owner):
        return f"navigation:{bot.id}:{owner}"

    def protected(self, owner, ident):
        return bool(
            self.store.rows(
                "SELECT 1 FROM notifications WHERE owner_id=? AND telegram_message_id=? "
                "UNION ALL SELECT 1 FROM operation_receipts WHERE owner_id=? AND telegram_message_id=? LIMIT 1",
                (owner, ident, owner, ident),
            )
        )

    async def enter(self, message, owner, state, action):
        bot = message.bot
        key = self.key(bot, owner)
        panel = self.store.setting(key, {})
        # A real callback supplies Telegram's own message, independently proving
        # a previously unacknowledged send. No text matching or blind resend.
        if panel.get("sending") and getattr(message.from_user, "id", None) == bot.id:
            markup = getattr(message, "reply_markup", None)
            if markup and markup.model_dump(mode="json") == panel.get("pending_markup"):
                panel.update(
                    id=message.message_id,
                    kind=panel.get("pending_kind"),
                    token=panel.get("pending_token"),
                    sending=False,
                )
                self.store.set_setting(key, panel)
        if action.startswith("nav:close"):
            await state.clear()
            await self.close(bot, owner)
            return None, None
        if action.startswith("nav:back:"):
            if action.split(":")[-1] != panel.get("token"):
                raise Conflict("Кнопка возврата устарела. Откройте актуальную панель")
            history = panel.get("history", [])
            previous = history.pop() if history else {"route": "home", "context": {}}
            await state.clear()
            await state.set_data(previous["context"])
            panel.update(history=history, route=previous["route"], restoring=True)
            action = previous["route"]
        elif action == "home":
            # /start explicitly recovers a known panel, never a pending mutation.
            panel.update(history=[], route="home")
        elif safe_route(action):
            if panel.get("route") and panel["route"] != action:
                panel["history"] = [
                    *panel.get("history", []),
                    {"route": panel["route"], "context": panel.get("context", {})},
                ][-20:]
            panel["route"] = action
        panel["closed"] = False
        self.store.set_setting(key, panel)
        return PanelMessage(message, self, owner, state, action), action

    def allowed(self, message, owner, action):
        if action in {"gft:stop", "promo:stop"}:
            # Stopping future spending is safe even from an old or unacknowledged UI.
            return True
        panel = self.store.setting(self.key(message.bot, owner), {})
        if panel.get("closed"):
            return False
        markup = getattr(message, "reply_markup", None)
        if (
            panel.get("sending")
            and getattr(message.from_user, "id", None) == message.bot.id
            and markup
            and markup.model_dump(mode="json") == panel.get("pending_markup")
            and action.startswith(("nav:back:", "nav:close"))
        ):
            return True
        if (
            panel.get("sending")
            and not safe_route(action)
            and not action.startswith(("nav:close", "nav:back:"))
            and action not in {"gft:stop", "promo:stop"}
        ):
            return False
        if panel.get("id") and panel["id"] != message.message_id:
            return safe_route(action) or action.startswith(("compose:", "ff:start:"))
        return True

    async def close(self, bot, owner):
        key = self.key(bot, owner)
        panel = self.store.setting(key, {})
        panel["closed"] = True
        self.store.set_setting(key, panel)
        ident = panel.get("id")
        if not ident or self.protected(owner, ident):
            return
        try:
            await bot.edit_message_reply_markup(chat_id=owner, message_id=ident, reply_markup=None)
            result = await bot.delete_message(chat_id=owner, message_id=ident)
            panel["deletion"] = "CONFIRMED" if result is True else "UNKNOWN"
            if result is True:
                panel.update(id=None, kind=None)
        except Exception:  # noqa: BLE001 -- closed admission persists even if Telegram outcome is unknown
            panel["deletion"] = "UNKNOWN"
        self.store.set_setting(key, panel)

    async def render(
        self,
        message,
        owner,
        state,
        action,
        *,
        text=None,
        photo=None,
        caption=None,
        reply_markup=None,
        **kwargs,
    ):
        bot, key = message.bot, self.key(message.bot, owner)
        lock = self.store.navigation_locks.setdefault((bot.id, owner), asyncio.Lock())
        async with lock:
            panel = self.store.setting(key, {})
            if panel.get("sending"):
                event(
                    "WARNING",
                    "Панель: результат отправки неизвестен; автоматического повтора нет. Нажмите кнопку полученного экрана или явно восстановите только меню командой /panel_reset",
                    level=30,
                )
                return None
            markup = reply_markup.model_copy(deep=True) if reply_markup else keyboard([], home=False)
            token = secrets.token_hex(6)
            for row in markup.inline_keyboard:
                for button in row:
                    if button.callback_data == "nav:close":
                        button.callback_data = "nav:close:" + token
            if panel.get("history") and safe_route(action):
                # Dynamic parent follows the actual origin; explicit destination buttons remain explicit.
                parent = panel["history"][-1]["route"]
                markup.inline_keyboard = [
                    row
                    for row in markup.inline_keyboard
                    if not (
                        len(row) == 1
                        and (
                            row[0].callback_data == "home"
                            or (
                                row[0].callback_data == parent
                                and row[0].text.startswith(("⬅", "К ", "Назад", "Отмена"))
                            )
                        )
                    )
                ]
                from aiogram.types import InlineKeyboardButton

                markup.inline_keyboard.append(
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:back:" + token)]
                )
            ident, kind = panel.get("id"), "photo" if photo is not None else "text"
            if ident and self.protected(owner, ident):
                ident = None  # Proofs and notifications are never adopted as panels.
            try:
                if ident and kind == "text" and panel.get("kind") == "text":
                    result = await bot.edit_message_text(
                        text=text, chat_id=owner, message_id=ident, reply_markup=markup, **kwargs
                    )
                elif ident and kind == "photo":
                    result = await bot.edit_message_media(
                        chat_id=owner,
                        message_id=ident,
                        media=InputMediaPhoto(media=photo, caption=caption),
                        reply_markup=markup,
                    )
                else:
                    result = None
            except TelegramBadRequest as error:
                if "message is not modified" in error.message.lower():
                    result = message
                elif any(
                    s in error.message.lower()
                    for s in ("message to edit not found", "message can't be edited")
                ):
                    result = None
                    if "message to edit not found" in error.message.lower():
                        ident = None
                else:
                    raise
            if result is None:
                if ident:
                    # Deactivate before replacing. A timeout here must not send a replacement.
                    await bot.edit_message_reply_markup(chat_id=owner, message_id=ident, reply_markup=None)
                panel["sending"] = True
                panel.update(
                    pending_markup=markup.model_dump(mode="json"), pending_kind=kind, pending_token=token
                )
                self.store.set_setting(key, panel)
                result = (
                    await bot.send_photo(owner, photo, caption=caption, reply_markup=markup, **kwargs)
                    if photo is not None
                    else await bot.send_message(owner, text, reply_markup=markup, **kwargs)
                )
                old, ident = ident, result.message_id
                panel.update(id=ident, sending=False)
                self.store.set_setting(key, panel)
                if old:
                    try:
                        await bot.delete_message(owner, old)
                    except Exception:  # noqa: BLE001 -- known old panel already has no keyboard; no deletion retry
                        panel["deletion"] = "UNKNOWN"
            panel.update(
                id=ident,
                kind=kind,
                token=token,
                restoring=False,
                context={k: v for k, v in (await state.get_data()).items() if k in CONTEXT},
            )
            self.store.set_setting(key, panel)
            return result


class PanelMessage:
    """Existing production panels share rendering without rewriting their business handlers."""

    def __init__(self, raw, navigation, owner, state, action):
        self.raw, self.navigation, self.owner, self.state, self.action = raw, navigation, owner, state, action

    def __getattr__(self, name):
        return getattr(self.raw, name)

    async def answer(self, text, **kwargs):
        return await self.navigation.render(
            self.raw, self.owner, self.state, self.action, text=text, **kwargs
        )

    async def edit_text(self, text, **kwargs):
        return await self.answer(text, **kwargs)

    async def answer_photo(self, photo, **kwargs):
        return await self.navigation.render(
            self.raw, self.owner, self.state, self.action, photo=photo, **kwargs
        )
