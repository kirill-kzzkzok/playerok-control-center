"""Bound callback acknowledgement before FSM isolation; never retry UI actions."""

import asyncio
import re
import time

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError

from app.security.logging import event as log_event
from app.telemetry import finish, measurements, record

ACK_TIMEOUT = 2
ACTION_MAX_WAIT = 10


# Only static routing words are diagnostic; never callback identifiers or input.
TRACE_GROUPS = frozenset(
    [
        "home",
        "items",
        "item",
        "iv",
        "lp",
        "ap",
        "auto",
        "stock",
        "ar",
        "chat",
        "chatpage",
        "chatmedia",
        "orders",
        "order",
        "od",
        "settings",
        "profile",
        "sp",
        "tools",
        "finance",
        "fin",
        "providers",
        "stats",
        "sd",
        "sr",
        "st",
        "promotions",
        "promo",
        "payp",
        "pp",
        "gft",
        "launch",
        "ln",
        "nav",
        "ops",
        "result",
        "sync",
        "attention",
        "create",
        "editname",
        "editdesc",
        "discount",
    ]
)
TRACE_VERBS = frozenset(
    [
        "view",
        "show",
        "item",
        "details",
        "history",
        "list",
        "open",
        "add",
        "edit",
        "commit",
        "check",
        "stop",
        "enable",
        "arm",
        "new",
        "menu",
        "settings",
        "reply",
        "close",
        "back",
    ]
)


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
        "quickreplies",
        "gft:home",
        "gft:catalog",
        "gft:bindings",
        "gft:choose",
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
            "lp:method:",
            "lp:messages:",
            "lp:completion:",
            "draft:",
            "finance:",
            "fin:options:",
            "auto:item:",
            "platform:item:",
            "status:view:",
            "relist:item:",
            "auto:check:",
            "stock:view:",
            "lp:item:",
            "lp:details:",
            "lp:ready:",
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
        from app.telegram.event_isolation import card_scope, scope_for

        started = time.perf_counter()
        values = {}
        query = getattr(event, "callback_query", None)
        parts = str(getattr(query, "data", "") or "").split(":")
        group = parts[0] if parts[0] in TRACE_GROUPS else "other"
        verb = parts[1] if len(parts) > 1 and parts[1] in TRACE_VERBS else None
        values["context"] = {"callback_group": group, "action": group + (":" + verb if verb else "")}
        token = measurements.set(values)
        scope = card_scope.set(scope_for(self.access.store, event, data["bot"].id))
        try:
            return await self.dispatch(handler, event, data)
        finally:
            finish(started, values)
            measurements.reset(token)
            card_scope.reset(scope)

    async def dispatch(self, handler, event, data):
        query = getattr(event, "callback_query", None)
        if query is not None:
            if not self.access.authorized(query):
                await acknowledge(query, text="Доступ закрыт", show_alert=True)
                return None
            started = time.perf_counter()
            data["callback_ack"] = await acknowledge(query)
            record("ack", time.perf_counter() - started)
            data["callback_ack"]["isolation_started"] = time.perf_counter()
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
            if "isolation_started" in data["callback_ack"]:
                record("fsm", time.perf_counter() - data["callback_ack"]["isolation_started"])
        return await handler(event, data)


def install_callback_ack(dispatcher, access):
    from app.telegram.event_isolation import CardEventIsolation

    dispatcher.fsm.events_isolation = CardEventIsolation()
    # Public middleware registration API: acknowledge before the per-owner FSM
    # lock, but leave FSM isolation and the normal owner guard intact.
    dispatcher.update.outer_middleware.unregister(dispatcher.fsm)
    dispatcher.update.outer_middleware(CallbackAcknowledgements(access))
    dispatcher.update.outer_middleware(dispatcher.fsm)
    dispatcher.update.outer_middleware(CallbackAdmission())
