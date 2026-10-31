from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery

from app.telegram.panel_tracking import panel_owner


class OwnerMiddleware(BaseMiddleware):
    def __init__(self, owners, store=None, first_start_claim=False):
        self.owners = frozenset(owners)
        self.store = store
        self.first_start_claim = first_start_claim

    def authorized(self, event):
        user = getattr(event, "from_user", None)
        message = event.message if isinstance(event, CallbackQuery) else event
        chat = getattr(message, "chat", None)
        return bool(
            user
            and chat
            and chat.type == "private"
            and (
                user.id in self.owners
                or (
                    self.store
                    and self.store.rows("SELECT user_id FROM administrators WHERE user_id=?", (user.id,))
                )
            )
        )

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        message = event.message if isinstance(event, CallbackQuery) else event
        chat = getattr(message, "chat", None)
        private = chat is not None and chat.type == "private"
        allowed = user is not None and user.id in self.owners
        if private and user and self.store:
            allowed = allowed or bool(
                self.store.rows("SELECT user_id FROM administrators WHERE user_id=?", (user.id,))
            )
            text = getattr(message, "text", "") or ""
            if (
                not allowed
                and self.first_start_claim
                and not isinstance(event, CallbackQuery)
                and text.split(" ", 1)[0].split("@")[0] == "/start"
            ):
                allowed = self.store.claim_first_owner(user.id)
        if user is None or not allowed or not private:
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ закрыт", show_alert=True)
            return None
        token = panel_owner.set(user.id)
        try:
            return await handler(event, data)
        finally:
            panel_owner.reset(token)
