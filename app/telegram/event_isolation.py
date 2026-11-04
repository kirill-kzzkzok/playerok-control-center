"""Serialize each durable card, without holding other cards behind remote reads."""

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace

from aiogram.fsm.storage.memory import SimpleEventIsolation

card_scope = ContextVar("event_card_scope", default=None)


def scope_for(store, update, bot_id):
    from app.telegram.navigation_context import callback_panel_key, panel_key

    query = update.callback_query
    message = query.message if query else update.message
    actor = query.from_user if query else getattr(message, "from_user", None)
    if not actor or not message or message.chat.type != "private" or message.chat.id != actor.id:
        return None
    primary = f"navigation:{bot_id}:{actor.id}"
    if query:
        return (
            callback_panel_key(store, bot_id, actor.id, message)
            if hasattr(message, "reply_markup")
            else primary
        )
    if (message.text or "").split(" ", 1)[0].split("@")[0] in {"/start", "/status", "/panel_reset"}:
        return primary
    reply = message.reply_to_message
    if reply and getattr(reply.from_user, "id", None) == bot_id:
        return panel_key(store, bot_id, actor.id, reply.message_id)
    return store.setting(f"navigation:active:{bot_id}:{actor.id}", primary)


class CardEventIsolation(SimpleEventIsolation):
    @asynccontextmanager
    async def lock(self, key):
        scope = card_scope.get()
        async with super().lock(replace(key, destiny=scope) if scope else key):
            yield
