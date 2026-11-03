import asyncio

from app.database.store import now
from app.services.delivery_chains import DeliveryChains


class ChainWorker:
    """Advance existing sequences only; never enroll historical orders automatically."""

    def __init__(self, store, api):
        self.store, self.service = store, DeliveryChains(store, api)

    def once(self):
        key = "chains:advance_cursor:" + str(self.service.api.account.id)
        with self.store.transaction():
            rows = self.store.rows(
                "SELECT order_id FROM delivery_chains WHERE status='ACTIVE' "
                "AND order_id>? ORDER BY order_id LIMIT 50",
                (self.store.setting(key, ""),),
            )
            if not rows and self.store.setting(key, ""):
                rows = self.store.rows(
                    "SELECT order_id FROM delivery_chains WHERE status='ACTIVE' ORDER BY order_id LIMIT 50"
                )
            self.store.set_setting(key, rows[-1]["order_id"] if len(rows) == 50 else "")
            return sum(self.service.advance(row["order_id"]) for row in rows)

    async def run(self, stop):
        while not stop.is_set():
            try:
                self.once()
                self.store.set_setting("chains:health", {"status": "OK", "at": now()})
            except Exception as error:  # noqa: BLE001 -- persist safe worker health, retain durable sequence
                self.store.set_setting(
                    "chains:health", {"status": "ERROR", "code": type(error).__name__, "at": now()}
                )
            try:
                await asyncio.wait_for(stop.wait(), 2)
            except TimeoutError:
                pass
