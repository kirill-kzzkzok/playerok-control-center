import time
from contextvars import ContextVar

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.types import Message

panel_owner = ContextVar("panel_owner", default=None)


class PanelTracking(BaseRequestMiddleware):
    """Track only successful responses inside an authorized UI handler, without text."""

    def __init__(self, store):
        self.store = store

    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        owner = panel_owner.get()
        if (
            owner is not None
            and isinstance(result, Message)
            and result.chat.type == "private"
            and result.chat.id == owner
            and method.__api_method__
            in {
                "sendMessage",
                "sendPhoto",
                "sendDocument",
                "editMessageText",
                "editMessageCaption",
                "editMessageReplyMarkup",
            }
        ):
            with self.store.transaction() as db:
                if db.execute("SELECT 1 FROM administrators WHERE user_id=?", (owner,)).fetchone():
                    db.execute(
                        "INSERT INTO panel_messages(bot_id,owner_id,message_id,created_at,touched_at) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(bot_id,owner_id,message_id) DO UPDATE SET touched_at=excluded.touched_at",
                        (bot.id, owner, result.message_id, result.date.timestamp(), time.time()),
                    )
        return result
