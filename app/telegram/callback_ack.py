"""Bound callback acknowledgement before FSM isolation; never retry UI actions."""

import asyncio
import time

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError

from app.security.logging import event as log_event

ACK_TIMEOUT = 2
ACTION_MAX_WAIT = 10


async def acknowledge(query, **kwargs):
    status = "transport"
    try:
        async with asyncio.timeout(ACK_TIMEOUT):
            await query.answer(request_timeout=ACK_TIMEOUT, **kwargs)
        status = "ok"
    except TelegramBadRequest as error:
        status = (
            "stale"
            if any(s in error.message.lower() for s in ("query is too old", "query id is invalid"))
            else "rejected"
        )
    except (TelegramNetworkError, TimeoutError):
        pass
    except TelegramAPIError:
        status = "rejected"
    if status != "ok":
        log_event("WARNING", "Telegram callback ACK: " + status + "; автоматического повтора нет", level=30)
    return {"status": status, "checked_at": time.monotonic()}


def navigation(action):
    # Explicit read/local-navigation allowlist, never a catch-all prefix for
    # confirmation, payment, refund, send, resume or operation reconciliation.
    return action in {
        "home",
        "more",
        "tools",
        "attention",
        "profile",
        "profile:check",
        "profile:details",
        "nav:close",
        "gft:history",
        "gft:settings",
        "diagnostics",
        "settings",
        "items",
        "chats",
        "orders",
        "create",
        "drafts",
        "automation",
        "promotions",
        "promo:stop",
        "quickreplies",
        "gft:home",
        "gft:stop",
        "gft:catalog",
        "gft:bindings",
        "gft:choose",
    } or action.startswith(
        (
            "item:",
            "lp:item:",
            "nav:back:",
            "nav:recover:",
            "nav:close:",
            "replypreview:",
            "ar:preview:",
            "payp:item:",
            "iv:",
            "tools:",
            "sr:",
            "gft:details:",
            "gft:bindings:",
            "chatmore:",
            "compose:",
            "od:details:",
            "order:",
            "chat:",
            "stats:",
            "result:list:",
            "result:show:",
            "delivered:",
            "gft:job:",
            "gft:list:",
            "gft:item:",
            "launch:open:",
            "launch:from:",
            "launch:item:",
            "stock:list:",
        )
    )


class CallbackAcknowledgements(BaseMiddleware):
    def __init__(self, access):
        self.access = access

    async def __call__(self, handler, event, data):
        query = getattr(event, "callback_query", None)
        if query is not None:
            if not self.access.authorized(query):
                await acknowledge(query, text="Доступ закрыт", show_alert=True)
                return None
            data["callback_ack"] = await acknowledge(query)
        try:
            return await handler(event, data)
        except TelegramNetworkError:
            # A timed-out send/edit may have succeeded. No second send and no
            # rerun of the action; persisted FSM/Outbox results remain authoritative.
            event_log = "Telegram UI: ошибка транспорта; результат отправки неизвестен. "
            log_event("WARNING", event_log + "Откройте /start или результаты операций; повтора нет", level=30)
            return None


def install_callback_ack(dispatcher, access):
    # Public middleware registration API: acknowledge before the per-owner FSM
    # lock, but leave FSM isolation and the normal owner guard intact.
    dispatcher.update.outer_middleware.unregister(dispatcher.fsm)
    dispatcher.update.outer_middleware(CallbackAcknowledgements(access))
    dispatcher.update.outer_middleware(dispatcher.fsm)
