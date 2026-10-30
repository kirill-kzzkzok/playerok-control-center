"""Personal-account MTProto only. Paid RPCs have no automatic retry/reconnect."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from telethon import TelegramClient, errors, functions, types

from app.gifts.core import telegram_username
from app.security.instance import InstanceLock


class GiftFailure(ValueError):
    """Allowlisted machine code, never a reflected RPC/request/username."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class InvalidRecipient(GiftFailure):
    pass


@dataclass(frozen=True)
class Recipient:
    username: str = field(repr=False)
    user_id: int = field(repr=False)
    access_hash: int = field(repr=False)

    @property
    def peer(self):
        return types.InputPeerUser(self.user_id, self.access_hash)


@dataclass(frozen=True)
class PreparedGift:
    recipient: Recipient
    gift_id: int
    stars: int
    form_id: int
    invoice: object = field(repr=False)


class PersonalGiftSender:
    def __init__(self, settings, client=None):
        self.settings, self.client = settings, client
        self.lock = asyncio.Lock()
        self.me = None
        self.session_lock = None

    async def connect(self):
        async with self.lock:
            if self.me is not None and getattr(self.client, "is_connected", lambda: True)():
                return
            if self.client is None:
                if not self.settings.api_id or not self.settings.api_hash:
                    raise GiftFailure("SESSION_NOT_CONFIGURED")
                self.session_lock = InstanceLock(self.settings.session_path + ".lock")
                self.session_lock.__enter__()
                # No RPC body/recipient/session data from Telethon reaches application logs.
                logger = logging.getLogger("pcc.private_mtproto")
                logger.addHandler(logging.NullHandler())
                logger.propagate = False
                self.client = TelegramClient(
                    self.settings.session_path,
                    self.settings.api_id,
                    self.settings.api_hash,
                    request_retries=0,
                    connection_retries=0,
                    auto_reconnect=False,
                    flood_sleep_threshold=0,
                    raise_last_call_error=True,
                    base_logger=logger,
                )
            await self.client.connect()
            if not await self.client.is_user_authorized():
                await self.client.disconnect()
                raise GiftFailure("SESSION_UNAUTHORIZED")
            me = await self.client.get_me()
            if not isinstance(me, types.User) or me.bot or me.deleted:
                await self.client.disconnect()
                raise GiftFailure("PERSONAL_ACCOUNT_REQUIRED")
            self.me = me

    async def close(self):
        try:
            if self.client:
                await self.client.disconnect()
        finally:
            self.me = None
            if self.session_lock:
                self.session_lock.__exit__(None, None, None)
                self.session_lock = None

    async def resolve(self, username):
        normalized = telegram_username(username)
        await self.connect()
        try:
            # Bypass get_entity's entity cache; the username must still refer to this user now.
            resolved = await self.client(functions.contacts.ResolveUsernameRequest(normalized[1:]))
        except (errors.UsernameInvalidError, errors.UsernameNotOccupiedError) as exc:
            raise InvalidRecipient(
                "USERNAME_INVALID"
                if isinstance(exc, errors.UsernameInvalidError)
                else "USERNAME_NOT_OCCUPIED"
            ) from None
        if not isinstance(resolved.peer, types.PeerUser):
            raise GiftFailure("RECIPIENT_NOT_USER")
        users = [u for u in resolved.users if isinstance(u, types.User) and u.id == resolved.peer.user_id]
        if len(users) != 1:
            raise GiftFailure("RECIPIENT_AMBIGUOUS")
        user = users[0]
        names = [user.username or ""] + [u.username for u in user.usernames or [] if u.active]
        if (
            user.bot
            or user.min
            or user.deleted
            or user.id == self.me.id
            or not user.access_hash
            or normalized[1:].casefold() not in {n.casefold() for n in names}
        ):
            raise GiftFailure("RECIPIENT_UNSUPPORTED")
        return Recipient(normalized, user.id, user.access_hash)

    async def catalog(self):
        await self.connect()
        value = await self.client(functions.payments.GetStarGiftsRequest(hash=0))
        if not isinstance(value, types.payments.StarGifts):
            raise GiftFailure("CATALOG_UNAVAILABLE")
        return value.gifts

    async def balance(self):
        await self.connect()
        value = await self.client(functions.payments.GetStarsStatusRequest(peer=types.InputPeerSelf()))
        balance = value.balance
        if not isinstance(balance, types.StarsAmount) or balance.amount < 0 or balance.nanos < 0:
            raise GiftFailure("BALANCE_UNAVAILABLE")
        return balance.amount

    async def prepare(self, gift_id, approved_stars, recipient, reference):
        catalog = await self.catalog()
        gift = next((g for g in catalog if isinstance(g, types.StarGift) and g.id == gift_id), None)
        if (
            gift is None
            or gift.sold_out
            or gift.auction
            or (gift.limited_per_user and (gift.per_user_remains or 0) < 1)
        ):
            raise GiftFailure("GIFT_UNAVAILABLE")
        if gift.require_premium and not self.me.premium:
            raise GiftFailure("PREMIUM_REQUIRED")
        if gift.stars != approved_stars:
            raise GiftFailure("PRICE_CHANGED")
        full = await self.client(
            functions.users.GetFullUserRequest(
                id=types.InputUser(recipient.user_id, recipient.access_hash),
            )
        )
        if not isinstance(full, types.users.UserFull) or full.full_user.id != recipient.user_id:
            raise GiftFailure("RECIPIENT_PERMISSIONS_UNKNOWN")
        restrictions = full.full_user.disallowed_gifts
        if full.full_user.blocked or (
            restrictions
            and (
                (gift.limited and restrictions.disallow_limited_stargifts)
                or (not gift.limited and restrictions.disallow_unlimited_stargifts)
            )
        ):
            raise GiftFailure("RECIPIENT_GIFTS_FORBIDDEN")
        permission = await self.client(functions.payments.CheckCanSendGiftRequest(gift_id=gift_id))
        if not isinstance(permission, types.payments.CheckCanSendGiftResultOk):
            raise GiftFailure("GIFT_RESTRICTED")
        if await self.balance() < gift.stars:
            raise GiftFailure("BALANCE_TOO_LOW")
        invoice = types.InputInvoiceStarGift(
            peer=recipient.peer,
            gift_id=gift_id,
            hide_name=False,
            include_upgrade=False,
            message=types.TextWithEntities(reference, []),
        )
        form = await self.client(functions.payments.GetPaymentFormRequest(invoice=invoice))
        if (
            not isinstance(form, types.payments.PaymentFormStarGift)
            or not form.form_id
            or form.invoice.currency != "XTR"
            or not form.invoice.prices
            or any(p.amount < 0 for p in form.invoice.prices)
            or sum(p.amount for p in form.invoice.prices) != approved_stars
        ):
            raise GiftFailure("PAYMENT_FORM_MISMATCH")
        return PreparedGift(recipient, gift_id, gift.stars, form.form_id, invoice)

    async def pay(self, prepared):
        if not self.settings.payment_enabled:
            raise GiftFailure("GIFT_PAYMENTS_DISABLED")
        result = await self.client(
            functions.payments.SendStarsFormRequest(
                form_id=prepared.form_id,
                invoice=prepared.invoice,
            )
        )
        if not isinstance(result, types.payments.PaymentResult):
            raise GiftFailure("PAYMENT_NOT_CONFIRMED")
        return result

    def matching_receipt(self, message, job):
        action = getattr(message, "action", None)
        date = getattr(message, "date", None)
        if (
            not isinstance(message, types.MessageService)
            or not message.out
            or not isinstance(message.peer_id, types.PeerUser)
            or message.peer_id.user_id != job["recipient_id"]
            or (
                getattr(message, "from_id", None) is not None
                and message.from_id != types.PeerUser(job["payer_id"])
            )
            or not isinstance(action, types.MessageActionStarGift)
            or not isinstance(action.gift, types.StarGift)
            or action.gift.id != int(job["gift_id"])
            or action.refunded
            or not action.message
            or action.message.text != job["reference"]
            or not message.id
            or not date
            or date.tzinfo is None
            or date < datetime.fromisoformat(job["spent_at"]).replace(microsecond=0)
        ):
            return None
        return {
            "message_id": message.id,
            "recipient_id": job["recipient_id"],
            "gift_id": job["gift_id"],
            "date": date.astimezone(UTC).isoformat(),
            "reference": job["reference"],
        }

    async def receipt(self, job, result=None):
        """Independent read, also safe for explicit UNCERTAIN reconciliation. No payment RPC."""
        await self.connect()
        if self.me.id != job["payer_id"]:
            raise GiftFailure("PERSONAL_ACCOUNT_CHANGED")
        peer = types.InputPeerUser(job["recipient_id"], int(job["recipient_access_hash"]))
        ids = []
        for update in getattr(getattr(result, "updates", None), "updates", []):
            message = getattr(update, "message", None)
            if self.matching_receipt(message, job):
                ids.append(message.id)
        messages = (
            await self.client.get_messages(peer, ids=ids)
            if ids
            else await self.client.get_messages(peer, limit=100)
        )
        matches = [r for msg in messages if (r := self.matching_receipt(msg, job))]
        if len(matches) != 1:
            raise GiftFailure("DELIVERY_NOT_CONFIRMED")
        return matches[0]
