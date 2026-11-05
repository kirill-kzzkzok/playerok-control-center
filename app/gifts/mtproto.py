"""Personal-account MTProto only. Paid RPCs have no automatic retry/reconnect."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from telethon import TelegramClient, errors, functions, types

from app.gifts.core import telegram_username
from app.localvpn import ProxyUnavailable, mtproto_proxy, require_proxy
from app.security.instance import InstanceLock
from app.security.logging import event
from app.telegram.system_tunnel import HappConnectionTcpFull, HappRoute, happ_required


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
    route_generation: int | None = None
    connection: object | None = field(default=None, repr=False)


async def drain_cleanup(coro):
    """A caller timeout must not release the session while Telethon still closes it."""
    task = asyncio.create_task(coro)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    task.result()
    if interrupted:
        raise asyncio.CancelledError


class PersonalGiftSender:
    def __init__(self, settings, client=None):
        self.settings, self.client = settings, client
        self.lock = asyncio.Lock()
        self.me = None
        self.session_lock = None
        self.closed = False
        self.owns_client = client is None
        self.happ_guard = (
            HappRoute()
            if client is None and happ_required(getattr(settings, "telegram_transport", "legacy"))
            else None
        )
        self.needs_disconnect = False
        self.route_generation = None
        self.connection = None

    @staticmethod
    def read_proxy():
        try:
            return require_proxy()
        except ProxyUnavailable:
            raise GiftFailure("PROXY_UNAVAILABLE") from None

    def current_route(self):
        if self.happ_guard:
            try:
                return self.happ_guard.check()
            except OSError:
                raise GiftFailure("PROXY_UNAVAILABLE") from None
        return (
            self.read_proxy().state["generation"] if self.settings.telegram_transport == "localvpn" else None
        )

    async def _disconnect(self):
        self.me = None
        self.connection = None
        self.route_generation = None
        if self.client is not None and self.needs_disconnect:
            await self.client.disconnect()
            self.needs_disconnect = False
            if self.owns_client:
                self.client = None

    async def connect(self):
        async with self.lock:
            if self.closed:
                raise GiftFailure("SESSION_CLOSED")
            try:
                generation = self.current_route()
            except GiftFailure:
                if self.needs_disconnect:
                    await drain_cleanup(self._disconnect())
                raise
            if (
                self.me is not None
                and self.route_generation == generation
                and getattr(self.client, "is_connected", lambda: True)()
            ):
                return
            if self.needs_disconnect:
                await drain_cleanup(self._disconnect())
                generation = self.current_route()
            if self.client is None:
                if not self.settings.api_id or not self.settings.api_hash:
                    raise GiftFailure("SESSION_NOT_CONFIGURED")
                if self.session_lock is None:
                    lock = InstanceLock(self.settings.session_path + ".lock")
                    lock.__enter__()
                    self.session_lock = lock
                # No RPC body/recipient/session data from Telethon reaches application logs.
                logger = logging.getLogger("pcc.private_mtproto")
                if not logger.handlers:
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
                    proxy=mtproto_proxy(self.settings.telegram_transport),
                    **({"connection": HappConnectionTcpFull} if self.happ_guard else {}),
                )
            self.needs_disconnect = True  # Even a partial connect can own send/receive tasks.
            try:
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    raise GiftFailure("SESSION_UNAUTHORIZED")
                me = await self.client.get_me()
                if not isinstance(me, types.User) or me.bot or me.deleted:
                    raise GiftFailure("PERSONAL_ACCOUNT_REQUIRED")
                if self.current_route() != generation:
                    raise GiftFailure("PROXY_ROUTE_CHANGED")
            except (Exception, asyncio.CancelledError):
                await drain_cleanup(self._disconnect())
                raise
            self.me = me
            self.connection = object()
            self.route_generation = generation
            event("INFO", "Личный Telegram подключён")

    async def close(self):
        self.closed = True
        await drain_cleanup(self._close())

    async def _close(self):
        async with self.lock:
            connected = self.me is not None
            await self._disconnect()
            if self.session_lock:
                self.session_lock.__exit__(None, None, None)
                self.session_lock = None
            if connected:
                event("INFO", "Личный Telegram отключён")

    async def _retire(self, connection):
        async with self.lock:
            if self.connection is connection:
                await self._disconnect()

    async def _read(self, call):
        connection, client = self.connection, self.client
        if connection is None or client is None:
            raise ConnectionError("MTProto connection changed before read")
        try:
            return await call(client)
        except (OSError, TimeoutError, asyncio.CancelledError):
            # A cancelled RPC does not necessarily clear Telethon.is_connected.
            # Retire exactly this connection, never a newer replacement.
            await drain_cleanup(self._retire(connection))
            raise

    async def _read_rpc(self, request):
        return await self._read(lambda client: client(request))

    async def resolve(self, username):
        normalized = telegram_username(username)
        await self.connect()
        own_id = self.me.id
        try:
            # Bypass get_entity's entity cache; the username must still refer to this user now.
            resolved = await self._read_rpc(functions.contacts.ResolveUsernameRequest(normalized[1:]))
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
            or user.id == own_id
            or not user.access_hash
            or normalized[1:].casefold() not in {n.casefold() for n in names}
        ):
            raise GiftFailure("RECIPIENT_UNSUPPORTED")
        return Recipient(normalized, user.id, user.access_hash)

    async def catalog(self):
        await self.connect()
        value = await self._read_rpc(functions.payments.GetStarGiftsRequest(hash=0))
        if not isinstance(value, types.payments.StarGifts):
            raise GiftFailure("CATALOG_UNAVAILABLE")
        return value.gifts

    async def balance(self):
        await self.connect()
        value = await self._read_rpc(functions.payments.GetStarsStatusRequest(peer=types.InputPeerSelf()))
        balance = value.balance
        if not isinstance(balance, types.StarsAmount) or balance.amount < 0 or balance.nanos < 0:
            raise GiftFailure("BALANCE_UNAVAILABLE")
        return balance.amount

    async def prepare(self, gift_id, approved_stars, recipient, reference):
        catalog = await self.catalog()
        connection, generation = self.connection, self.route_generation
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
        full = await self._read_rpc(
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
        permission = await self._read_rpc(functions.payments.CheckCanSendGiftRequest(gift_id=gift_id))
        if not isinstance(permission, types.payments.CheckCanSendGiftResultOk):
            raise GiftFailure("GIFT_RESTRICTED")
        if await self.balance() < gift.stars:
            raise GiftFailure("BALANCE_TOO_LOW")
        invoice = types.InputInvoiceStarGift(
            peer=recipient.peer,
            gift_id=gift_id,
            hide_name=False,
            include_upgrade=False,
        )
        form = await self._read_rpc(functions.payments.GetPaymentFormRequest(invoice=invoice))
        if (
            not isinstance(form, types.payments.PaymentFormStarGift)
            or not form.form_id
            or form.invoice.currency != "XTR"
            or not form.invoice.prices
            or any(p.amount < 0 for p in form.invoice.prices)
            or sum(p.amount for p in form.invoice.prices) != approved_stars
        ):
            raise GiftFailure("PAYMENT_FORM_MISMATCH")
        if self.connection is not connection or self.current_route() != generation:
            raise GiftFailure("PROXY_ROUTE_CHANGED")
        return PreparedGift(recipient, gift_id, gift.stars, form.form_id, invoice, generation, connection)

    async def check_quantities(self, gifts):
        from collections import Counter

        counts = Counter(int(g["gift_id"]) for g in gifts)
        catalog = await self.catalog()
        for gift_id, count in counts.items():
            gift = next((g for g in catalog if isinstance(g, types.StarGift) and g.id == gift_id), None)
            if not gift or gift.sold_out or gift.auction:
                raise GiftFailure("GIFT_UNAVAILABLE")
            if (
                gift.limited_per_user
                and (gift.per_user_remains or 0) < count
                or gift.limited
                and (gift.availability_remains or 0) < count
            ):
                raise GiftFailure("GIFT_QUANTITY_UNAVAILABLE")

    async def pay(self, prepared):
        async with self.lock:
            if not self.settings.payment_enabled:
                raise GiftFailure("GIFT_PAYMENTS_DISABLED")
            if self.closed or self.me is None:
                raise GiftFailure("SESSION_CLOSED")
            if prepared.connection is not None and prepared.connection is not self.connection:
                raise GiftFailure("PROXY_ROUTE_CHANGED")
            if self.settings.telegram_transport == "localvpn":
                proxy = require_proxy()
                if await proxy.confirm() != prepared.route_generation:
                    raise GiftFailure("PROXY_ROUTE_CHANGED")
            if self.happ_guard and self.current_route() != prepared.route_generation:
                raise GiftFailure("PROXY_ROUTE_CHANGED")
            if self.closed:
                raise GiftFailure("SESSION_CLOSED")
            try:
                result = await self.client(
                    functions.payments.SendStarsFormRequest(
                        form_id=prepared.form_id,
                        invoice=prepared.invoice,
                    )
                )
            except (OSError, TimeoutError, asyncio.CancelledError):
                # Transport cleanup is NOT payment recovery. The durable caller
                # retains UNKNOWN and must never resend this paid request.
                await drain_cleanup(self._disconnect())
                raise
        if not isinstance(result, types.payments.PaymentResult):
            raise GiftFailure("PAYMENT_NOT_CONFIRMED")
        return result

    def _gift_message(self, message, job):
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

    def acknowledgement(self, job, result):
        """Only one gift message in this paid RPC's reply may anchor a new send.

        This is correlation, not delivery proof; independent history must still
        confirm the exact ID. Missing/ambiguous ACK is never guessed from history.
        """
        candidates = set()
        for update in getattr(getattr(result, "updates", None), "updates", []):
            message = getattr(update, "message", None)
            if self._gift_message(message, job) and not getattr(message.action.message, "text", ""):
                candidates.add(message.id)
        if len(candidates) != 1:
            raise GiftFailure("DELIVERY_CORRELATION_UNKNOWN")
        return candidates.pop()

    def matching_receipt(self, message, job):
        receipt = self._gift_message(message, job)
        if not receipt:
            return None
        if job.get("receipt_mode", "REFERENCE") == "MESSAGE_ID":
            if not job.get("ack_message_id") or message.id != job["ack_message_id"]:
                return None
            if getattr(message.action.message, "text", ""):
                return None
        elif not message.action.message or message.action.message.text != job["reference"]:
            return None
        return receipt

    async def receipt(self, job, result=None):
        """Independent read, also safe for explicit UNCERTAIN reconciliation. No payment RPC."""
        await self.connect()
        if self.me.id != job["payer_id"]:
            raise GiftFailure("PERSONAL_ACCOUNT_CHANGED")
        peer = types.InputPeerUser(job["recipient_id"], int(job["recipient_access_hash"]))
        # Old reference-bearing sends retain read-only recovery. New blank sends
        # must have their RPC-derived ID durably saved before entering this method.
        ids = [job["ack_message_id"]] if job.get("ack_message_id") else []
        if job.get("receipt_mode") == "MESSAGE_ID" and not ids:
            raise GiftFailure("DELIVERY_CORRELATION_UNKNOWN")
        messages = await self._read(
            lambda client: client.get_messages(peer, ids=ids) if ids else client.get_messages(peer, limit=100)
        )
        matches = [r for msg in messages if (r := self.matching_receipt(msg, job))]
        if len(matches) != 1:
            raise GiftFailure("DELIVERY_NOT_CONFIRMED")
        return matches[0]
