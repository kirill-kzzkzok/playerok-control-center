"""Select a callback's physical card before invoking existing UI handlers."""

import json
from contextvars import ContextVar
from dataclasses import replace

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext

from app.telegram.callback_ack import navigation
from app.telegram.storage import replace_context

source_panel = ContextVar("source_panel", default=None)


def waiting_forms(store, bot_id, owner):
    primary = f"navigation:{bot_id}:{owner}"
    forms = {}
    for row in store.rows(
        "SELECT key FROM fsm WHERE state IS NOT NULL AND json_valid(key) AND json_extract(key,'$[0]')=? AND json_extract(key,'$[1]')=? AND json_extract(key,'$[2]')=?",
        (bot_id, owner, owner),
    ):
        destiny = json.loads(row["key"])[5]
        panel = store.setting(destiny, {}) if destiny.startswith(primary) else {}
        if panel.get("id") and not panel.get("closed"):
            forms[destiny] = panel
    return forms


def panel_key(store, bot_id, owner, message_id):
    primary = f"navigation:{bot_id}:{owner}"
    if store.setting(primary, {}).get("id") == message_id:
        return primary
    known = store.rows(
        "SELECT key FROM settings WHERE key GLOB ? AND json_extract(value,'$.id')=?",
        (primary + ":message:*", message_id),
    )
    if len(known) == 1:
        return known[0]["key"]
    return f"{primary}:message:{message_id}"


def callback_panel_key(store, bot_id, owner, message):
    """The same identity for isolation and rendering, including lost send ACKs."""
    from app.telegram.navigation import Navigation

    primary = f"navigation:{bot_id}:{owner}"
    key = panel_key(store, bot_id, owner, message.message_id)
    if not store.setting(key):
        candidates = [(primary, store.setting(primary, {}))]
        candidates += [
            (row["key"], json.loads(row["value"]))
            for row in store.rows(
                "SELECT key,value FROM settings WHERE key GLOB ?", (primary + ":message:*",)
            )
        ]
        observed = [candidate for candidate, panel in candidates if Navigation.observed(message, panel)]
        if len(observed) == 1:
            key = observed[0]
    if key != primary and not store.setting(primary, {}).get("id") and not message.reply_markup:
        key = primary
    return key


class MessageNavigation(BaseMiddleware):
    def __init__(self, store):
        self.store = store

    async def __call__(self, handler, event, data):
        state = data.get("state")
        if state is None:
            return await handler(event, data)
        callback = hasattr(event, "data") and hasattr(event, "message")
        message = event.message if callback else event
        if message is None or not hasattr(message, "bot"):
            return await handler(event, data)
        owner, bot = event.from_user.id, message.bot
        if message.chat.type != "private" or message.chat.id != owner:
            return None
        if callback and not hasattr(message, "reply_markup"):
            return await bot.send_message(
                owner,
                "Исходное сообщение недоступно. Откройте карточку заново через /start. Другие экраны сохранены.",
            )
        if (
            callback
            and data.get("callback_ack", {}).get("status") in {"stale", "rejected"}
            and not navigation(event.data or "")
        ):
            return await bot.send_message(
                owner,
                "Кнопка устарела или отклонена Telegram. Действие не выполнено. Откройте нужную карточку заново; соседние экраны сохранены.",
            )
        primary = f"navigation:{bot.id}:{owner}"
        active_key = f"navigation:active:{bot.id}:{owner}"
        active = self.store.setting(active_key, primary)
        key = active
        if callback:
            key = callback_panel_key(self.store, bot.id, owner, message)
            if key != primary and not self.store.setting(key):
                if not navigation(event.data or "") and not (event.data or "").startswith("cc:"):
                    return await bot.send_message(
                        owner,
                        "Кнопка устарела. Откройте карточку заново через /start; действие не выполнено.",
                    )
                self.store.set_setting(
                    key,
                    {
                        "id": message.message_id,
                        "kind": "photo" if message.photo else "text",
                        "route": None,
                        "history": [],
                        "context": {},
                    },
                )
        elif (message.text or "").split(" ", 1)[0].split("@")[0] in {"/start", "/status", "/panel_reset"}:
            key = primary
        elif (message.text or "").split(" ", 1)[0].split("@")[0] != "/cancel":
            # The last click is not proof of the intended recipient of plain
            # text when independent cards are both waiting for input.
            forms = waiting_forms(self.store, bot.id, owner)
            reply = message.reply_to_message
            if reply:
                selected = panel_key(self.store, bot.id, owner, reply.message_id)
                if getattr(reply.from_user, "id", None) != bot.id or selected not in forms:
                    return await bot.send_message(
                        owner,
                        "Это сообщение не связано с открытой формой. Ответьте средствами Telegram на сообщение с нужной формой; данные не отправлены покупателю.",
                    )
                key = selected
            elif len(forms) > 1:
                return await bot.send_message(
                    owner,
                    "Открыто несколько форм. Ответьте средствами Telegram на сообщение с нужной формой и пришлите данные ещё раз. Ничего не отправлено покупателю; ваш текст сохранён в чате.",
                )
            elif len(forms) == 1:
                key = next(iter(forms))
        token = source_panel.set((bot.id, owner, key))
        try:
            from app.telegram.navigation import Navigation

            if (
                callback
                and not (event.data or "").startswith(
                    ("cc:", "chat:", "compose:", "reply:", "readchat:", "chatmore:", "chatclose:")
                )
                and not Navigation(self.store).allowed(message, owner, event.data or "")
            ):
                return await handler(event, data)
            alias = f"navigation:fsm_active:{bot.id}:{owner}"
            if not self.store.setting(alias):
                # One-time migration of the pre-existing single durable FSM.
                # Thereafter each card has ONE canonical row: no snapshots of
                # consumed confirmations can resurrect after a crash.
                old_state, old_data = await state.get_state(), await state.get_data()
                old_key = state.storage.key(state.key)
                prior = FSMContext(state.storage, replace(state.key, destiny=primary))
                with self.store.transaction():
                    await replace_context(prior, old_state, old_data)
                    self.store.execute("DELETE FROM fsm WHERE key=?", (old_key,))
                    self.store.set_setting(alias, key)
            else:
                self.store.set_setting(alias, key)
            self.store.set_setting(active_key, key)
            if callback and not (event.data or "").startswith(
                ("cc:", "chat:", "readchat:", "chatmore:", "chatpage:")
            ):
                # A notification card used as a form/another screen is no longer
                # the live conversation view. The next incoming event may open
                # one new notice; it must not overwrite this owner's form.
                self.store.execute(
                    "UPDATE chat_cards SET state='CLOSED',hint='',busy_until=0 WHERE owner_id=? AND account_id=? AND telegram_message_id=? AND state='OPEN'",
                    (owner, self.store.setting("playerok:account_id", ""), message.message_id),
                )
            data["state"] = FSMContext(state.storage, replace(state.key, destiny=key))
            data["raw_state"] = await data["state"].get_state()
            return await handler(event, data)
        finally:
            source_panel.reset(token)
