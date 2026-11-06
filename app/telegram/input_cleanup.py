"""Best-effort cleanup only after durable acceptance of owner FSM input."""

import asyncio
import logging
from contextvars import ContextVar

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError

service_input = ContextVar("service_input", default=None)
input_accepted = ContextVar("input_accepted", default=False)


def accept_input():
    input_accepted.set(True)


async def cleanup_input():
    message = service_input.get()
    if message is None or not input_accepted.get():
        return
    # Consume before I/O: a timeout must not cause a second deletion attempt.
    service_input.set(None)
    try:
        async with asyncio.timeout(3):
            await message.delete()
    except (TelegramAPIError, TimeoutError, OSError) as error:
        logging.getLogger(__name__).debug("Service input deletion not confirmed: %s", type(error).__name__)


class InputCleanup(BaseMiddleware):
    def __init__(self, store):
        self.store = store

    async def __call__(self, handler, event, data):
        state = data.get("state")
        consume = bool(
            state
            and event.from_user
            and event.chat.type == "private"
            and event.chat.id == event.from_user.id
            and self.store.rows("SELECT 1 FROM administrators WHERE user_id=?", (event.from_user.id,))
            and not (event.text or "").startswith("/")
            and await state.get_state()
            and await state.get_state() != "chatcard:reply"
        )
        token = service_input.set(event if consume else None)
        accepted = input_accepted.set(False)
        try:
            return await handler(event, data)
        finally:
            try:
                await cleanup_input()
            finally:
                service_input.reset(token)
                input_accepted.reset(accepted)
