"""One owner UI panel; durable rendering outcomes, separate from business receipts."""

import asyncio
import secrets
from copy import deepcopy

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InputMediaPhoto

from app.database.store import Conflict
from app.security.logging import event
from app.telegram.input_cleanup import cleanup_input
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
        "promo:catalog",
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
            "lp:item:",
            "lp:ready:",
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
            "auto:check:",
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
    "list_catalog_at",
    "gift_picker",
    "promotion_picker",
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
        if self.observed(message, panel):
            panel.update(panel.get("pending_view", {}))
            panel.update(
                id=message.message_id,
                kind=panel.get("pending_kind"),
                token=panel.get("pending_token"),
                sending=False,
                editing=False,
                closed=False,
            )
            for name in ("recovery_token", "recovery_id", "recovery_markup", "recovery_kind"):
                panel.pop(name, None)
            self.store.set_setting(key, panel)
        if action.startswith("nav:close"):
            await state.clear()
            await self.close(bot, owner)
            return None, None
        proposed = {
            name: deepcopy(panel.get(name, [] if name == "history" else None))
            for name in ("route", "history", "context")
        }
        restoring = False
        if action.startswith("nav:back:"):
            if action.split(":")[-1] != panel.get("token"):
                raise Conflict("Кнопка возврата устарела. Откройте актуальную панель")
            history = proposed["history"]
            previous = history.pop() if history else {"route": "home", "context": {}}
            await state.clear()
            await state.set_data(previous["context"])
            proposed.update(history=history, route=previous["route"])
            restoring = True
            action = previous["route"]
        elif action == "home":
            proposed.update(history=[], route="home")
        elif safe_route(action):
            if proposed.get("route") and proposed["route"] != action:
                proposed["history"] = [
                    *proposed["history"],
                    {"route": proposed["route"], "context": proposed.get("context") or {}},
                ][-20:]
            proposed["route"] = action
        return PanelMessage(message, self, owner, state, action, proposed, restoring), action

    @staticmethod
    def observed(message, panel):
        markup = getattr(message, "reply_markup", None)
        return bool(
            (panel.get("sending") or panel.get("editing"))
            and getattr(message.from_user, "id", None) == message.bot.id
            and markup
            and markup.model_dump(mode="json") == panel.get("pending_markup")
        )

    async def feedback(self, message, owner, text):
        # Independent owner feedback; never adopt it or mutate navigation/FSM.
        return await message.bot.send_message(owner, text)

    async def recovery_prompt(self, message, owner):
        key = self.key(message.bot, owner)
        panel = self.store.setting(key, {})
        token = secrets.token_hex(8)
        markup = keyboard([[("🔄 Восстановить меню", "nav:recover:" + token)]], home=False)
        text = "⚠️ Последний экран не подтверждён Telegram. Восстановить меню? Действия магазина повторяться не будут; старые подтверждения станут недействительными."
        kind = (
            panel.get("pending_kind")
            if panel.get("editing") and not panel.get("sending")
            else panel.get("kind")
        )
        panel.update(
            recovery_token=token,
            recovery_id=None,
            recovery_markup=markup.model_dump(mode="json"),
            recovery_kind=kind or "text",
        )
        self.store.set_setting(key, panel)
        ident, result = panel.get("id"), None
        if ident and not self.protected(owner, ident):
            for attempt in range(2):
                try:
                    if panel["recovery_kind"] == "photo":
                        result = await message.bot.edit_message_caption(
                            chat_id=owner, message_id=ident, caption=text, reply_markup=markup
                        )
                    else:
                        result = await message.bot.edit_message_text(
                            chat_id=owner, message_id=ident, text=text, reply_markup=markup
                        )
                    break
                except TelegramBadRequest as error:
                    reason = error.message.lower()
                    if attempt == 0 and ("there is no text" in reason or "there is no caption" in reason):
                        panel["recovery_kind"] = "photo" if panel["recovery_kind"] == "text" else "text"
                        self.store.set_setting(key, panel)
                        continue
                    if any(
                        v in reason
                        for v in (
                            "message to edit not found",
                            "message can't be edited",
                            "there is no text",
                            "there is no caption",
                        )
                    ):
                        break
                    raise
        if result is None:
            panel["recovery_kind"] = "text"
            self.store.set_setting(key, panel)
            result = await message.bot.send_message(owner, text, reply_markup=markup)
        panel["recovery_id"] = result.message_id
        self.store.set_setting(key, panel)
        return result

    async def recover(self, message, owner, state, token):
        key = self.key(message.bot, owner)
        panel = self.store.setting(key, {})
        if token != panel.get("recovery_token") or not self.allowed(message, owner, "nav:recover:" + token):
            raise Conflict("Восстановление уже выполнено. Откройте /start")
        await state.clear()
        self.store.set_setting(
            key,
            {
                "id": panel.get("recovery_id") or message.message_id,
                "kind": panel.get("recovery_kind", "text"),
                "route": None,
                "history": [],
                "abandoned_send": True,
            },
        )

    def allowed(self, message, owner, action):
        if action in {"gft:stop", "promo:stop"}:
            # Stopping future spending is safe even from an old or unacknowledged UI.
            return True
        panel = self.store.setting(self.key(message.bot, owner), {})
        if action.startswith(("reply:", "compose:", "readchat:", "chat:")):
            from app.services.replies import Replies

            if Replies(self.store).origin(owner, message.message_id, action.split(":", 1)[1]):
                return True
        if action.startswith("nav:recover:"):
            markup = getattr(message, "reply_markup", None)
            proof = (
                getattr(message.from_user, "id", None) == message.bot.id
                and markup
                and markup.model_dump(mode="json") == panel.get("recovery_markup")
            )
            return action.split(":")[-1] == panel.get("recovery_token") and (
                message.message_id == panel.get("recovery_id") or bool(proof)
            )
        if self.observed(message, panel) and (
            safe_route(action) or action.startswith(("nav:back:", "nav:close"))
        ):
            return True
        if panel.get("closed"):
            return False
        if (
            (panel.get("sending") or panel.get("editing"))
            and not safe_route(action)
            and not action.startswith(("nav:close", "nav:back:"))
            and action not in {"gft:stop", "promo:stop"}
        ):
            return False
        if panel.get("id") and panel["id"] != message.message_id:
            return safe_route(action) or action.startswith("ff:start:")
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
        proposal=None,
        reply_markup=None,
        **kwargs,
    ):
        await cleanup_input()
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
                return await self.recovery_prompt(message, owner)
            view = deepcopy(proposal or {name: panel.get(name) for name in ("route", "history", "context")})
            view["context"] = {k: v for k, v in (await state.get_data()).items() if k in CONTEXT}
            markup = reply_markup.model_copy(deep=True) if reply_markup else keyboard([], home=False)
            token = secrets.token_hex(6)
            for row in markup.inline_keyboard:
                for button in row:
                    if button.callback_data == "nav:close":
                        button.callback_data = "nav:close:" + token
            if view.get("history") and safe_route(action):
                # Dynamic parent follows the actual origin; explicit destination buttons remain explicit.
                parent = view["history"][-1]["route"]
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
            panel.update(
                pending_view=view,
                pending_markup=markup.model_dump(mode="json"),
                pending_kind=kind,
                pending_token=token,
                editing=bool(ident),
            )
            self.store.set_setting(key, panel)
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
                    for s in (
                        "message to edit not found",
                        "message can't be edited",
                        "there is no text",
                        "there is no caption",
                    )
                ):
                    result = None
                    ident = (
                        None  # Definite rejection permits a replacement, never a replay of business actions.
                    )
                else:
                    raise
            if result is None:
                if ident:
                    # Deactivate before replacing. A timeout here must not send a replacement.
                    try:
                        await bot.edit_message_reply_markup(
                            chat_id=owner, message_id=ident, reply_markup=None
                        )
                    except TelegramBadRequest as error:
                        if not any(
                            v in error.message.lower()
                            for v in (
                                "message to edit not found",
                                "message can't be edited",
                                "message is not modified",
                            )
                        ):
                            raise
                        ident = None
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
                panel.update(view)
                panel.update(id=ident, kind=kind, token=token, closed=False, editing=False, sending=False)
                for name in ("recovery_token", "recovery_id", "recovery_markup", "recovery_kind"):
                    panel.pop(name, None)
                self.store.set_setting(key, panel)
                if old:
                    try:
                        await bot.delete_message(owner, old)
                    except Exception:  # noqa: BLE001 -- known old panel already has no keyboard; no deletion retry
                        panel["deletion"] = "UNKNOWN"
            for name in ("recovery_token", "recovery_id", "recovery_markup", "recovery_kind"):
                panel.pop(name, None)
            panel.update(view)
            panel.update(
                closed=False,
                editing=False,
                sending=False,
                id=ident,
                kind=kind,
                token=token,
                restoring=False,
                context={k: v for k, v in (await state.get_data()).items() if k in CONTEXT},
            )
            self.store.set_setting(key, panel)
            self.track_result(owner, panel, markup)
            return result

    def track_result(self, owner, panel, markup):
        routes = [
            b.callback_data
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data and b.callback_data.startswith("result:show:")
        ]
        if len(routes) == 1:
            self.store.execute(
                "UPDATE operation_receipts SET view_message_id=?,view_token=?,rendered_status=NULL WHERE operation_id=? AND owner_id=?",
                (panel["id"], panel["token"], routes[0].split(":", 2)[2], owner),
            )


class PanelMessage:
    """Existing production panels share rendering without rewriting their business handlers."""

    def __init__(self, raw, navigation, owner, state, action, proposed=None, restoring=False):
        self.raw, self.navigation, self.owner, self.state, self.action = raw, navigation, owner, state, action
        self.proposed, self.navigation_restoring = proposed, restoring

    def __getattr__(self, name):
        return getattr(self.raw, name)

    async def answer(self, text, **kwargs):
        return await self.navigation.render(
            self.raw, self.owner, self.state, self.action, text=text, proposal=self.proposed, **kwargs
        )

    async def edit_text(self, text, **kwargs):
        return await self.answer(text, **kwargs)

    async def answer_photo(self, photo, **kwargs):
        return await self.navigation.render(
            self.raw, self.owner, self.state, self.action, photo=photo, proposal=self.proposed, **kwargs
        )
