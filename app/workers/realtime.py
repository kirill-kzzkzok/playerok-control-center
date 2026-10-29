import asyncio
import json
import random
import time
import uuid
from contextlib import asynccontextmanager

import aiohttp

from app.database.store import now
from app.playerok.adapter import APIError

WS_URL = "wss://ws.playerok.com/graphql"
TOPICS = ("chatUpdated", "chatMarkedAsRead", "userUpdated")
SUBSCRIPTIONS = {
    "chatUpdated": "subscription chatUpdated($filter: ChatFilter) { chatUpdated(filter: $filter) { id lastMessage { id deal { id } } } }",
    "chatMarkedAsRead": "subscription chatMarkedAsRead($filter: ChatFilter) { chatMarkedAsRead(filter: $filter) { id } }",
    "userUpdated": "subscription userUpdated($userId: UUID) { userUpdated(userId: $userId) { ... on User { id } ... on UserFragment { id } } }",
}


async def origin_guard(request, handler):
    # Applied to redirected handshake requests too: never forward credentials elsewhere.
    if str(request.url) != WS_URL:
        raise APIError("WS_ENDPOINT_FORBIDDEN")
    return await handler(request)


class Realtime:
    """Read-only subscriptions produce durable API hints, never mutations."""

    def __init__(self, store, account, synchronizer, *, connector=None, idle=30, pong_timeout=10):
        self.store, self.account, self.sync = store, account, synchronizer
        self.connector = connector or self.connect
        self.idle, self.pong_timeout = idle, pong_timeout

    def health(self, status, **fields):
        self.store.set_setting("realtime:health", {"status": status, "at": now(), **fields})

    @asynccontextmanager
    async def connect(self):
        headers = {
            "cookie": "; ".join(f"{key}={value}" for key, value in self.account.cookies.items()),
            "user-agent": self.account.user_agent,
        }
        async with (
            aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
                cookie_jar=aiohttp.DummyCookieJar(),
                middlewares=(origin_guard,),
                trust_env=True,
            ) as session,
            session.ws_connect(
                WS_URL,
                protocols=("graphql-transport-ws",),
                origin="https://playerok.com",
                ssl=True,
                max_msg_size=1_000_000,
                timeout=aiohttp.ClientWSTimeout(ws_close=3),
            ) as ws,
        ):
            if ws.protocol != "graphql-transport-ws":
                raise APIError("WS_SUBPROTOCOL_MISMATCH")
            yield ws

    @staticmethod
    async def packet(ws, timeout):
        # aiohttp may restart its receive timer while handling native ping frames.
        # Keep an absolute application deadline around the entire receive call.
        async with asyncio.timeout(timeout):
            frame = await ws.receive(timeout=timeout)
        if frame.type != aiohttp.WSMsgType.TEXT:
            if frame.type == aiohttp.WSMsgType.CLOSE and isinstance(frame.data, int):
                raise APIError(f"WS_CLOSED_{frame.data}")
            raise APIError("WS_CLOSED_OR_NON_TEXT")
        try:
            packet = json.loads(frame.data)
        except (ValueError, TypeError):
            raise APIError("WS_INVALID_JSON") from None
        if not isinstance(packet, dict) or not isinstance(packet.get("type"), str):
            raise APIError("WS_INVALID_PACKET")
        return packet

    def signal(self, packet, subscriptions=None):
        subscriptions = subscriptions if subscriptions is not None else {topic: topic for topic in TOPICS}
        topic, payload = subscriptions.get(packet.get("id")), packet.get("payload")
        if topic not in TOPICS or not isinstance(payload, dict) or payload.get("errors"):
            raise APIError("WS_SUBSCRIPTION_RESPONSE_INVALID")
        data = payload.get("data")
        value = data.get(topic) if isinstance(data, dict) else None
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("id"), str)
            or not 1 <= len(value["id"]) <= 100
        ):
            raise APIError("WS_EVENT_INCOMPLETE")
        if topic == "userUpdated":
            if value["id"] != self.account.id:
                raise APIError("WS_ACCOUNT_MISMATCH")
            self.sync.request("catchup", "*")
        else:
            self.sync.request("chat", value["id"])
            message = value.get("lastMessage")
            deal = message.get("deal") if isinstance(message, dict) else None
            if isinstance(deal, dict) and isinstance(deal.get("id"), str) and 1 <= len(deal["id"]) <= 100:
                self.sync.request("deal", deal["id"])

    async def serve(self, ws):
        if not self.account.id:
            raise APIError("WS_ACCOUNT_NOT_INITIALIZED")
        await ws.send_json(
            {
                "type": "connection_init",
                "payload": {
                    "x-gql-op": "ws-subscription",
                    "x-gql-path": "/chats/[id]",
                    "x-timezone-offset": -180,
                },
            }
        )
        async with asyncio.timeout(10):
            while True:
                packet = await self.packet(ws, 10)
                if packet["type"] == "ping":
                    await ws.send_json(
                        {"type": "pong", **({"payload": packet["payload"]} if "payload" in packet else {})}
                    )
                elif packet["type"] == "connection_ack":
                    break
                else:
                    raise APIError("WS_ACK_EXPECTED")
        subscriptions = {str(uuid.uuid4()): topic for topic in TOPICS}
        for ident, topic in subscriptions.items():
            variables = (
                {"userId": self.account.id}
                if topic == "userUpdated"
                else {"filter": {"userId": self.account.id}}
            )
            await ws.send_json(
                {
                    "id": ident,
                    "type": "subscribe",
                    "payload": {
                        "operationName": topic,
                        "query": SUBSCRIPTIONS[topic],
                        "variables": variables,
                        "extensions": {},
                    },
                }
            )
        self.sync.request("catchup", "*")
        self.health("CONNECTED")
        ping_at, pong_by = time.monotonic() + self.idle, None
        while True:
            deadline = pong_by if pong_by is not None else ping_at
            try:
                packet = await self.packet(ws, max(0.001, deadline - time.monotonic()))
            except TimeoutError:
                if pong_by is not None:
                    raise APIError("WS_PONG_TIMEOUT") from None
                await ws.send_json({"type": "ping"})
                pong_by = time.monotonic() + self.pong_timeout
                continue
            kind = packet["type"]
            if kind == "ping":
                await ws.send_json(
                    {"type": "pong", **({"payload": packet["payload"]} if "payload" in packet else {})}
                )
            elif kind == "pong":
                pong_by, ping_at = None, time.monotonic() + self.idle
                self.health("CONNECTED", pong_at=now())
            elif kind == "next":
                self.signal(packet, subscriptions)
            elif kind in {"complete", "error"}:
                raise APIError("WS_SUBSCRIPTION_TERMINATED")
            else:
                raise APIError("WS_UNEXPECTED_PACKET")
            if pong_by is not None and time.monotonic() >= pong_by:
                raise APIError("WS_PONG_TIMEOUT")

    async def connection(self):
        async with self.connector() as ws:
            await self.serve(ws)

    async def run(self, stop):
        failures = 0
        try:
            while not stop.is_set():
                self.health("CONNECTING", failures=failures)
                started = time.monotonic()
                tasks = [asyncio.create_task(self.connection()), asyncio.create_task(stop.wait())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    if stop.is_set():
                        break
                    await tasks[0]
                    raise APIError("WS_CONNECTION_ENDED")
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 -- never log raw frames/URLs/cookies
                    failures = 1 if time.monotonic() - started > 60 else failures + 1
                    code = (
                        error.code
                        if isinstance(error, APIError)
                        else f"WS_HTTP_{error.status}"
                        if isinstance(error, aiohttp.WSServerHandshakeError)
                        else "WS_TRANSPORT_ERROR"
                    )
                    self.health("RETRYING", code=code, failures=failures)
                    self.sync.request("catchup", "*")
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                delay = min(300, 2 ** min(failures, 8)) + random.uniform(0, 1)
                if code in {"WS_HTTP_401", "WS_HTTP_403", "WS_HTTP_429"}:
                    delay = max(300, delay)
                elif code in {
                    "WS_CLOSED_4400",
                    "WS_CLOSED_4401",
                    "WS_CLOSED_4403",
                    "WS_CLOSED_4500",
                    "WS_SUBSCRIPTION_TERMINATED",
                }:
                    delay = max(60, delay)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            self.health("STOPPED")
