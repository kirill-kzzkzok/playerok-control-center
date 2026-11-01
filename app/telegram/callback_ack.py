"""Bound callback acknowledgement before FSM isolation; never retry UI actions."""

import asyncio
import re
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
    if re.fullmatch(
        r"(?:w:[^:]+:(?:back|next|choice_page:\d+|preview_page:\d+|preview_return|edit:[a-z_]+|photo_preview:\d+|photos_done|field_keep|field_done|confirm_done|finance_open|finance_method|finance_cost|finance_done|finance_clear_cost|finance_clear_method|finance_refresh)|orderpage:\d+:[A-Z_]+|listnext:(?:items|chats):[^:]+|chatpage:[^:]+:(?:older|newer)|itemfilter:[A-Z_]+|il:(?:search|category|minimum|maximum|PREMIUM|DEFAULT|ALL|reset)|ops:(?:list:[A-Z_]+:\d+|show:[^:]+)|aq:(?:list:[a-z_]+:\d+|chain:[^:]+|target:[^:]+|evaluation:[^:]+)|ar:(?:menu|list|show:[^:]+)|(?:sd|st):\d+:[a-z_]+)",
        action,
    ):
        return True
    return action in {
        "home",
        "more",
        "tools",
        "attention",
        "profile",
        "providers",
        "sp:notifications",
        "sp:connections",
        "sp:security",
        "sp:fulfillment",
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
            "draft:",
            "finance:",
            "fin:options:",
            "auto:item:",
            "stock:view:",
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
            "reply:",
            "readchat:",
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


class CallbackAdmission(BaseMiddleware):
    """Start dispatch-age after FSM isolation, not while queued behind a reader."""

    async def __call__(self, handler, event, data):
        if data.get("callback_ack") is not None:
            data["callback_ack"]["admitted_at"] = time.monotonic()
        return await handler(event, data)


def install_callback_ack(dispatcher, access):
    # Public middleware registration API: acknowledge before the per-owner FSM
    # lock, but leave FSM isolation and the normal owner guard intact.
    dispatcher.update.outer_middleware.unregister(dispatcher.fsm)
    dispatcher.update.outer_middleware(CallbackAcknowledgements(access))
    dispatcher.update.outer_middleware(dispatcher.fsm)
    dispatcher.update.outer_middleware(CallbackAdmission())
