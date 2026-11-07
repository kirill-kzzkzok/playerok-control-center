"""One owner UI panel; durable rendering outcomes, separate from business receipts."""

import asyncio
import hashlib
import json
import secrets
import time
from copy import deepcopy

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, InputMediaPhoto

from app.database.store import Conflict
from app.security.logging import event
from app.telegram.errors import telegram_error
from app.telegram.input_cleanup import cleanup_input
from app.telegram.keyboards import keyboard
from app.telemetry import record


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
            "codes:home",
            "codes:page:",
            "codes:results:",
            "item:",
            "rd:check:",
            "rd:balance:",
            "rd:permissions:",
            "lp:limits:",
            "lp:item:",
            "lp:details:",
            "lp:method:",
            "lp:messages:",
            "lp:completion:",
            "lp:ready:",
            "iv:",
            "order:",
            "chat:",
            "compose:",
            "reply:",
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
            "platform:item:",
            "status:view:",
            "relist:item:",
            "auto:check:",
            "ops:list:",
            "ops:show:",
            "result:show:",
            "draft:",
            "launch:open:",
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
    "automation_draft",
    "logical_draft",
}


class Navigation:
    def __init__(self, store):
        self.store = store
        if not hasattr(store, "navigation_locks"):
            store.navigation_locks = {}

    def key(self, bot, owner):
        from app.telegram.navigation_context import source_panel

        scoped = source_panel.get()
        if scoped and scoped[:2] == (bot.id, owner):
            return scoped[2]
        return f"navigation:{bot.id}:{owner}"

    def protected(self, owner, ident):
        return bool(
            self.store.rows(
                "SELECT 1 FROM notifications WHERE owner_id=? AND telegram_message_id=? "
                "UNION ALL SELECT 1 FROM operation_receipts WHERE owner_id=? AND telegram_message_id=? "
                "UNION ALL SELECT 1 FROM chat_cards WHERE owner_id=? AND telegram_message_id=? LIMIT 1",
                (owner, ident, owner, ident, owner, ident),
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
            if action.count(":") == 2 and action.split(":")[-1] != panel.get("token"):
                raise Conflict("Кнопка закрытия устарела. Откройте текущую карточку")
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
        ):
            return False
        return not (panel.get("id") and panel["id"] != message.message_id)

    async def close(self, bot, owner):
        key = self.key(bot, owner)
        panel = self.store.setting(key, {})
        panel["closed"] = True
        self.store.set_setting(key, panel)
        ident = panel.get("id")
        if not ident:
            return
        if self.protected(owner, ident):
            try:
                await bot.edit_message_reply_markup(chat_id=owner, message_id=ident, reply_markup=None)
            except Exception:  # noqa: BLE001 -- keep the receipt and the durable closed gate
                panel["keyboard"] = "UNKNOWN"
                self.store.set_setting(key, panel)
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
        expected=None,
        reply_markup=None,
        **kwargs,
    ):
        await cleanup_input()
        bot, key = message.bot, self.key(message.bot, owner)
        lock = self.store.navigation_locks.setdefault((bot.id, owner, key), asyncio.Lock())
        waiting = time.perf_counter()
        async with lock:
            record("navigation_lock", time.perf_counter() - waiting)
            panel = self.store.setting(key, {})
            if expected is not None and panel != expected:
                return message
            if panel.get("sending"):
                event(
                    "WARNING",
                    "Панель: результат отправки неизвестен; автоматического повтора нет. Нажмите кнопку полученного экрана или явно восстановите только меню командой /panel_reset",
                    level=30,
                )
                return await self.recovery_prompt(message, owner)
            if panel.get("ui_retry_at", 0) > time.time():
                return message  # Explicit Telegram flood limit; no blind resend/re-execution.
            view = deepcopy(proposal or {name: panel.get(name) for name in ("route", "history", "context")})
            view["context"] = {k: v for k, v in (await state.get_data()).items() if k in CONTEXT}
            editing = (await state.get_data()).get("automation")
            if editing and not editing.get("logical_check"):
                # Keep editable values, never one-shot arm/commit capabilities.
                view["context"]["automation_draft"] = {
                    "account": editing.get("account"),
                    **{key: editing[key] for key in ("id", "revision", "values")},
                }
            draft = await state.get_data()
            if draft.get("logical_setup") and draft.get("logical_view"):
                logical = draft["logical_view"]
                view["context"]["logical_draft"] = {
                    "item": logical["item"],
                    "owner": owner,
                    "account": self.store.setting("playerok:account_id"),
                    "product": logical.get("product"),
                    "revision": logical["revision"],
                    "setup": draft["logical_setup"],
                    "legacy_revision": draft.get("logical_legacy_revision"),
                }
            # Incoming Telegram models carry a private mounted Bot. Copying
            # that object traverses its live session/middleware (including locks).
            markup = (
                InlineKeyboardMarkup.model_validate(reply_markup.model_dump(exclude_none=True))
                if reply_markup
                else keyboard([], home=False)
            )
            unchanged_route = (
                view.get("route") == panel.get("route")
                and view.get("history") == panel.get("history")
                and not panel.get("closed")
                and not panel.get("editing")
            )
            token = panel.get("token") if unchanged_route else None
            token = token or secrets.token_hex(6)
            for row in markup.inline_keyboard:
                for button in row:
                    if button.callback_data == "nav:close":
                        button.callback_data = "nav:close:" + token
            draft_card = action.startswith("item:") and any(
                (button.callback_data or "").startswith("launch:item:")
                for row in markup.inline_keyboard
                for button in row
            )
            has_return = any(
                len(row) == 1
                and (
                    row[0].text == "⬅️ Вернуться"
                    or (row[0].callback_data or "").startswith("nav:back:")
                    or draft_card
                    and row[0].callback_data == "items"
                    or action.startswith("launch:open:")
                    and row[0].text.startswith("⬅️ К товару")
                )
                for row in markup.inline_keyboard
            )
            if (
                view.get("history")
                and safe_route(action)
                and not has_return
                and not action.startswith(("chat:", "chatpage:"))
            ):
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
            fingerprint = (
                hashlib.sha256(
                    json.dumps(
                        {"text": text, "markup": markup.model_dump(mode="json"), "kwargs": kwargs},
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ).encode()
                ).hexdigest()
                if photo is None
                else None
            )
            if (
                ident
                and kind == "text"
                and panel.get("kind") == "text"
                and unchanged_route
                and fingerprint == panel.get("content_fingerprint")
                and getattr(message.from_user, "id", None) == bot.id
                and message.message_id == ident
            ):
                panel.update(view)
                self.store.set_setting(key, panel)
                return message
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
            except TelegramRetryAfter as error:
                panel["editing"] = False
                panel["ui_error"] = "RATE_LIMITED"
                panel["ui_retry_at"] = time.time() + error.retry_after
                for name in ("pending_view", "pending_markup", "pending_kind", "pending_token"):
                    panel.pop(name, None)
                self.store.set_setting(key, panel)
                raise
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
                    panel["editing"] = False
                    panel["ui_error"] = telegram_error(error)
                    for name in ("pending_view", "pending_markup", "pending_kind", "pending_token"):
                        panel.pop(name, None)
                    self.store.set_setting(key, panel)
                    raise
            if result is None:
                if expected is not None:
                    panel.update(closed=True, editing=False)
                    self.store.set_setting(key, panel)
                    return message
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
                if old and not self.protected(owner, old):
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
                content_fingerprint=fingerprint,
                restoring=False,
                context=view["context"],
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
