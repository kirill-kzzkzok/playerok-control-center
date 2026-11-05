"""Safe Telegram failure codes. Never expose exception text or request payloads."""

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

REJECTED_CONTENT = frozenset(
    {"INVALID_MESSAGE_CONTENT", "MESSAGE_TOO_LONG", "ENTITY_PARSE_ERROR", "INVALID_MESSAGE_ID"}
)
TERMINAL_EDIT = REJECTED_CONTENT | {"MESSAGE_NOT_FOUND", "MESSAGE_NOT_EDITABLE"}


class TelegramRequestNotStarted(TelegramNetworkError):
    """Raised only by local admission before handing a request to HTTP transport."""


def send_failure(error):
    """A local no-dispatch proof or returned rejection may permit another attempt."""
    if isinstance(error, TelegramRequestNotStarted):
        return "PENDING", "PROXY_UNAVAILABLE", 5
    if isinstance(error, TelegramRetryAfter):
        return "PENDING", "RATE_LIMITED", max(1, error.retry_after)
    if isinstance(error, TelegramForbiddenError):
        return "REJECTED", "SEND_FORBIDDEN", 0
    if isinstance(error, TelegramBadRequest):
        return "REJECTED", "SEND_REJECTED", 0
    return "UNCERTAIN", telegram_error(error), 0


def telegram_error(error):
    if isinstance(error, TelegramRequestNotStarted):
        return "PROXY_UNAVAILABLE"
    if isinstance(error, TelegramRetryAfter):
        return "RATE_LIMITED"
    if isinstance(error, TimeoutError):
        return "TIMEOUT"
    if isinstance(error, TelegramNetworkError):
        return "TIMEOUT" if "request timeout error" in error.message.lower() else "NETWORK_ERROR"
    if isinstance(error, TelegramBadRequest):
        text = error.message.lower()
        for code, phrases in (
            ("MESSAGE_NOT_MODIFIED", ("message is not modified",)),
            ("MESSAGE_NOT_FOUND", ("message to edit not found", "message to delete not found")),
            ("MESSAGE_NOT_EDITABLE", ("message can't be edited", "message can not be edited")),
            ("ENTITY_PARSE_ERROR", ("can't parse entities", "can't find end of the entity")),
            ("MESSAGE_TOO_LONG", ("message is too long", "caption is too long")),
            (
                "INVALID_MESSAGE_ID",
                ("message_id_invalid", "message identifier is not specified", "message id is invalid"),
            ),
            ("CALLBACK_EXPIRED", ("query is too old", "query id is invalid")),
            (
                "INVALID_MESSAGE_CONTENT",
                (
                    "message text is empty",
                    "there is no text",
                    "there is no caption",
                    "wrong type of the web page content",
                    "media_empty",
                    "wrong file identifier",
                ),
            ),
        ):
            if any(phrase in text for phrase in phrases):
                return code
        return "UNKNOWN_TELEGRAM_ERROR"
    if isinstance(error, (ValueError, TypeError)):
        return "LOCAL_MESSAGE_ERROR"
    return "UNKNOWN_TELEGRAM_ERROR"
