import asyncio
import logging
import random

from app.database.store import now
from app.playerok.adapter import APIError
from playerokapi.enums import ItemDealDirections


class Synchronizer:
    def __init__(self, store, api, orders):
        self.store, self.api, self.orders = store, api, orders
        self.health = {"status": "NOT_STARTED"}

    async def once(self):
        # Full available order history, never just the first page. API limits remain visible.
        count = 0
        async for page in self.api.pages("get_deals", "deals", direction=ItemDealDirections.OUT):
            for order in page:
                self.orders.ingest(order)
                count += 1
        self.store.set_setting("orders:last_complete_sync", now())
        async for page in self.api.pages("get_chats", "chats"):
            for chat in page:
                async for messages in self.api.pages("get_chat_messages", "messages", chat_id=chat["id"]):
                    for message in messages:
                        self.orders.ingest_message(chat["id"], message)
        self.health = {"status": "OK", "at": now(), "orders": count}
        self.store.set_setting("sync:health", self.health)

    async def run(self, stop):
        failures = 0
        while not stop.is_set():
            try:
                await self.once()
                failures = 0
            except APIError as error:
                failures += 1
                self.health = {"status": "ERROR", "code": error.code, "at": now()}
                self.store.set_setting("sync:health", self.health)
                logging.getLogger(__name__).warning("Synchronization failed: %s", error.code)
            except Exception as error:  # noqa: BLE001 -- isolate worker failure, persist health and back off
                failures += 1
                self.health = {"status": "ERROR", "code": "SYNC_SCHEMA_OR_STORAGE_ERROR", "at": now()}
                self.store.set_setting("sync:health", self.health)
                logging.getLogger(__name__).error(
                    "Synchronization stopped for this cycle: %s", type(error).__name__
                )
            delay = min(900, 60 * 2 ** min(failures, 4)) + random.uniform(0, 5)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                continue
