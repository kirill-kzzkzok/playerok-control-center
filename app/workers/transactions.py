import asyncio
import random
import time

from app.database.store import now
from app.playerok.adapter import APIError
from app.services.transactions import Transactions
from app.workers.pagination import PageScanner
from app.workers.supervisor import worker_cycle


class TransactionWorker:
    def __init__(self, store, api, *, pages_per_cycle=2):
        self.store, self.api = store, api
        self.scanner = PageScanner(store, api, pages_per_stream=pages_per_cycle)
        self.lock = asyncio.Lock()
        self.health = {"status": "NOT_STARTED"}

    async def once(self):
        async with self.lock:
            account_id = getattr(self.api.account, "id", None)
            if not isinstance(account_id, str) or not account_id:
                self.health = {"status": "NOT_AUTHORIZED", "at": now()}
                self.store.set_setting("transactions:health", self.health)
                return
            transactions = Transactions(self.store, account_id)
            backoff_key = f"transactions:{account_id}:not_before"
            failure_key = f"transactions:{account_id}:failures"
            not_before = self.store.setting(backoff_key, 0)
            if not_before > time.time():
                self.health = {"status": "BACKOFF", "at": now(), "retry_at": not_before}
                self.store.set_setting("transactions:health", self.health)
                return

            def guard(_):
                if self.api.account.id != account_id:
                    raise APIError("TRANSACTION_ACCOUNT_CHANGED")

            def ingest(entry):
                if self.api.account.id != account_id:
                    raise APIError("TRANSACTION_ACCOUNT_CHANGED")
                transactions.ingest(entry)
                self.store.set_setting("transactions:account_id", account_id)

            try:
                count, complete = await self.scanner.scan(
                    f"transactions:{account_id}", "get_transactions", "transactions", ingest, page_guard=guard
                )
                if self.api.account.id != account_id:
                    raise APIError("TRANSACTION_ACCOUNT_CHANGED")
                self.store.set_setting("transactions:account_id", account_id)
                self.health = {"status": "OK" if complete else "BACKFILL", "at": now(), "observations": count}
                self.store.set_setting(backoff_key, 0)
                self.store.set_setting(failure_key, 0)
            except APIError as error:
                self.health = {"status": "ERROR", "at": now(), "code": error.code}
                failures = self.store.setting(failure_key, 0) + 1
                delay = min(900, 5 * 2 ** min(failures, 8))
                retry_after = error.retry_after
                if isinstance(retry_after, (str, int)) and str(retry_after).isdigit():
                    delay = max(delay, int(retry_after))
                self.health["retry_at"] = time.time() + delay + random.uniform(0, 5)
                self.store.set_setting(backoff_key, self.health["retry_at"])
                self.store.set_setting(failure_key, failures)
            except (ValueError, TypeError, KeyError, AttributeError):
                self.health = {"status": "ERROR", "at": now(), "code": "TRANSACTION_SCHEMA_ERROR"}
            self.store.set_setting("transactions:health", self.health)
            if self.health["status"] in {"OK", "BACKFILL"}:
                from app.services.order_finance import cache_shop_profit

                cache_shop_profit(self.store, account_id, partial=self.health["status"] == "BACKFILL")

    async def run(self, stop):
        failures = 0
        while not stop.is_set():
            try:
                with worker_cycle(
                    self.store, "transactions", min(900, 60 * 2 ** min(failures + 1, 4))
                ) as cycle:
                    await self.once()
                    cycle["processed"] = self.health.get("observations", 0)
                    if self.health.get("status") in {"ERROR", "NOT_AUTHORIZED"}:
                        cycle["error_code"] = self.health.get("code", "ACCOUNT_NOT_VERIFIED")
                    cycle["retry_delay"] = max(
                        cycle["retry_delay"], self.health.get("retry_at", 0) - time.time()
                    )
            except Exception as error:  # noqa: BLE001 -- persist a sanitized worker boundary
                self.health = {
                    "status": "ERROR",
                    "at": now(),
                    "code": "TRANSACTION_WORKER_ERROR",
                    "error_type": type(error).__name__,
                }
                self.store.set_setting("transactions:health", self.health)
            failures = failures + 1 if self.health["status"] in {"ERROR", "NOT_AUTHORIZED"} else 0
            delay = min(900, 60 * 2 ** min(failures, 4)) + random.uniform(0, 5)
            delay = max(delay, self.health.get("retry_at", 0) - time.time())
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
