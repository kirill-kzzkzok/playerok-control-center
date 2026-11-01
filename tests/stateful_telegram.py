"""Stateful offline Bot API contract; no network or production credentials."""

from copy import deepcopy
from datetime import UTC, datetime

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import Chat, Message, PhotoSize, User

from tests.test_telegram_integration import TelegramSession


class StatefulTelegram(TelegramSession):
    def __init__(self):
        super().__init__()
        self.messages = {}
        self.sequence = 100
        self.acks = set()
        self.faults = {}

    def inject(self, method, phase="before"):
        self.faults[method] = phase

    async def make_request(self, bot, method, timeout=None):
        name = method.__api_method__
        fault = self.faults.pop(name, None)
        if fault == "before":
            self.calls.append(method)
            raise TelegramNetworkError(method=method, message="offline before ACK")
        if name == "answerCallbackQuery":
            self.calls.append(method)
            if method.callback_query_id in self.acks:
                raise TelegramBadRequest(method=method, message="query ID is invalid")
            self.acks.add(method.callback_query_id)
            return True
        if name not in {
            "sendMessage",
            "sendPhoto",
            "editMessageText",
            "editMessageCaption",
            "editMessageMedia",
            "editMessageReplyMarkup",
            "deleteMessage",
        }:
            return await super().make_request(bot, method, timeout)
        self.calls.append(method)
        creating = name in {"sendMessage", "sendPhoto"}
        if creating:
            self.sequence += 1
            ident = self.sequence
            value = {
                "kind": "photo" if name == "sendPhoto" else "text",
                "text": None,
                "caption": None,
                "reply_markup": None,
                "editable": True,
                "exists": True,
            }
        else:
            ident = method.message_id
            value = self.messages.get(ident)
            if not value or not value["exists"]:
                raise TelegramBadRequest(method=method, message="message to edit not found")
            if not value["editable"]:
                raise TelegramBadRequest(method=method, message="message can't be edited")
            value = deepcopy(value)
        before = deepcopy(value)
        if name in {"sendMessage", "editMessageText"}:
            if value["kind"] != "text":
                raise TelegramBadRequest(method=method, message="there is no text in the message to edit")
            value["text"] = method.text
        if name == "editMessageCaption" and value["kind"] != "photo":
            raise TelegramBadRequest(method=method, message="there is no caption in the message")
        if name in {"sendPhoto", "editMessageCaption"}:
            value["caption"] = method.caption
        if name == "editMessageMedia":
            value.update(kind="photo", text=None, caption=method.media.caption)
        if name == "deleteMessage":
            value["exists"] = False
        else:
            value["reply_markup"] = method.reply_markup
        if not creating and before == value:
            raise TelegramBadRequest(method=method, message="message is not modified")
        self.messages[ident] = value
        if fault == "after":
            raise TelegramNetworkError(method=method, message="offline applied but ACK lost")
        if name == "deleteMessage":
            return True
        return Message(
            message_id=ident,
            date=datetime.now(UTC),
            chat=Chat(id=method.chat_id, type="private"),
            from_user=User(id=bot.id, first_name="Bot", is_bot=True),
            text=value["text"],
            caption=value["caption"],
            reply_markup=value["reply_markup"],
            photo=[PhotoSize(file_id="p", file_unique_id="p", width=1, height=1)]
            if value["kind"] == "photo"
            else None,
        )
