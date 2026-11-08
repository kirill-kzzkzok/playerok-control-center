"""Bounded read-only catalog reconciliation before existing delivery enrollment."""

import asyncio
import json
import uuid
from datetime import UTC, datetime

from app.database.store import Conflict, encode, now, server_time
from app.playerok.adapter import APIError
from app.security.logging import event
from app.services.automation import future_order
from app.services.logical_products import (
    MODERATION_STATUSES,
    LogicalProducts,
    RelistConflict,
    critical,
    marker,
)
from app.services.order_management import compatible_detail, deal_details, deliverable
from app.services.orders import Orders
from app.workers.pagination import PageScanner
from playerokapi.enums import ItemStatuses

RATE_LIMIT_CODES = {"RATE_LIMITED", "HTTP_429"}


def require_listing_details(item):
    # The catalog classifies malformed markers per product after hydration.
    # A full title with a malformed marker is still a complete server response.
    if (
        not isinstance(item.get("name"), str)
        or not item["name"].strip()
        or not isinstance(item.get("description"), str)
        or item.get("price") is None
        or any(
            not isinstance(item.get(key), dict) or not item[key].get("id")
            for key in ("user", "category", "obtaining_type")
        )
    ):
        raise APIError("LOGICAL_CATALOG_ITEM_INCOMPLETE")


class LogicalProductWorker:
    def __init__(self, store, api):
        self.store, self.api = store, api
        self.service = LogicalProducts(store, api)
        if not hasattr(store, "logical_scan_lock"):
            store.logical_scan_lock = asyncio.Lock()
        if not hasattr(store, "logical_paid_wake"):
            store.logical_paid_wake = asyncio.Event()
        self.paid_wake = store.logical_paid_wake

    async def once(self):
        return await self.scan()

    async def scan(self, *, product_id=None):
        async with self.store.logical_scan_lock:
            return await self._scan(product_id=product_id)

    async def _scan(self, *, product_id=None):
        products = self.store.rows(
            "SELECT * FROM logical_products WHERE state='VERIFIED' AND "
            + ("id=?" if product_id else "enabled=1"),
            (product_id,) if product_id else (),
        )
        if not products:
            return 0
        connection = getattr(self.api, "connection", None)
        if connection is not None and not connection.reads_allowed:
            raise APIError("CONNECTION_NOT_VERIFIED")
        account = self.api.account.id
        if not account or account != self.store.setting("playerok:account_id"):
            raise Conflict("Аккаунт Playerok не подтверждён для сверки объявлений")
        key = "logical:scan:" + account
        progress = self.store.setting(key, {})
        if not progress:
            progress = {"generation": uuid.uuid4().hex, "phase": "LISTING", "started_at": now()}
            self.store.set_setting(key, progress)
        generation = progress["generation"]
        if (datetime.now(UTC) - datetime.fromisoformat(progress["started_at"])).total_seconds() > 600:
            self.store.execute("DELETE FROM sync_cursors WHERE key=?", (key + ":pages",))
            self.store.set_setting(key, {})
            self.store.set_setting(key + ":error", {"code": "SCAN_EXPIRED", "at": now()})
            return 0

        def ingest(item):
            if not isinstance(item.get("id"), str) or not item["id"]:
                raise APIError("LOGICAL_CATALOG_ITEM_ID_MISSING")
            self.store.execute(
                "INSERT INTO logical_scan_items(account_id,item_id,generation) VALUES(?,?,?) "
                "ON CONFLICT(account_id,item_id) DO UPDATE SET generation=excluded.generation,"
                "payload=CASE WHEN generation=excluded.generation THEN payload ELSE NULL END,"
                "checked_at=CASE WHEN generation=excluded.generation THEN checked_at ELSE NULL END",
                (account, item["id"], generation),
            )

        if progress["phase"] == "LISTING":
            _, complete = await PageScanner(self.store, self.api, pages_per_stream=2).scan(
                key + ":pages",
                "get_my_items",
                "items",
                ingest,
                statuses=[
                    ItemStatuses.APPROVED,
                    ItemStatuses.PENDING_APPROVAL,
                    ItemStatuses.PENDING_MODERATION,
                ],
                count=24,
            )
            if not complete:
                return 0
            progress["phase"] = "HYDRATING"
            self.store.set_setting(key, progress)
        pending = self.store.rows(
            "SELECT item_id FROM logical_scan_items WHERE account_id=? AND generation=? AND payload IS NULL ORDER BY item_id LIMIT 20",
            (account, generation),
        )
        for row in pending:
            item = await self.api.call("get_item", id=row["item_id"])
            if item.get("id") != row["item_id"] or (item.get("user") or {}).get("id") != account:
                raise APIError("LOGICAL_CATALOG_ACCOUNT_OR_ID_CHANGED")
            require_listing_details(item)
            self.store.execute(
                "UPDATE logical_scan_items SET payload=?,checked_at=? WHERE account_id=? AND item_id=? AND generation=?",
                (encode(item), now(), account, row["item_id"], generation),
            )
        if self.store.rows(
            "SELECT 1 FROM logical_scan_items WHERE account_id=? AND generation=? AND payload IS NULL LIMIT 1",
            (account, generation),
        ):
            return 0
        snapshot = [
            json.loads(row["payload"])
            for row in self.store.rows(
                "SELECT payload FROM logical_scan_items WHERE account_id=? AND generation=?",
                (account, generation),
            )
        ]
        # A newly relisted item may already be SOLD before the next active-list
        # scan. Fresh PAID deals supply candidate IDs; the marker never supplies
        # ownership or payment authority by itself.
        paid_items, snapshot_orders = {}, {}
        cursor = self.store.setting(key + ":paid_cursor", "")
        batch_start = cursor

        def platform_candidates(deal):
            try:
                code = marker((deal.get("item") or {}).get("name"))
            except ValueError:
                return []
            return [
                p
                for p in products
                if json.loads(p["config"]).get("platform_only") is True
                and p["code"] == code
                and future_order(deal.get("created_at"), p["armed_at"])
            ]

        def validate_candidate_order(deal, ident):
            candidates = platform_candidates(deal)
            if deal.get("is_automated") is not True and not candidates:
                return deliverable(deal, ident, account)
            from app.services.platform_orders import platform_sale

            if len(candidates) != 1:
                raise Conflict("Не подтверждена постоянная привязка выдачи Playerok")
            return platform_sale(deal, ident, account)

        pending_orders = self.store.rows(
            "SELECT o.id,o.item_id,o.payload FROM orders o WHERE (o.status='PAID' OR (o.status IN ('SENT','CONFIRMED','CONFIRMED_AUTOMATICALLY') AND json_extract(o.payload,'$.is_automated')=1)) AND o.id>? "
            "AND NOT EXISTS(SELECT 1 FROM logical_listings l WHERE l.item_id=o.item_id) ORDER BY o.id LIMIT 20",
            (cursor,),
        )
        for row in pending_orders:
            self.store.set_setting(key + ":paid_cursor", row["id"])
            local = json.loads(row["payload"])
            observed_date = server_time(local.get("created_at"))
            if observed_date and not any(future_order(observed_date, p["armed_at"]) for p in products):
                continue
            try:
                deal = await deal_details(
                    self.api,
                    row["id"],
                    full_item=True,
                    need_created=True,
                    need_platform_data=bool(platform_candidates(local)),
                )
                validate_candidate_order(deal, row["id"])
                if not server_time(deal.get("created_at")):
                    raise APIError("DELIVERY_SNAPSHOT_NOT_YET_AVAILABLE")
                if not any(future_order(deal["created_at"], p["armed_at"]) for p in products):
                    continue
                if (deal.get("item") or {}).get("id") != row["item_id"]:
                    raise Conflict("Предмет сделки изменился")
                try:
                    observed = await self.api.call("get_item", id=row["item_id"])
                except APIError as error:
                    if error.code != "HTTP_404":
                        raise
                    # Playerok deal.item can be a purchased snapshot whose ID
                    # is not a public item route. Read its full, exact deal.
                    detailed = await self.api.call("get_delivery_deal", deal_id=row["id"])
                    compatible_detail(deal, detailed)
                    deal = detailed
                    validate_candidate_order(deal, row["id"])
                    observed = deal.get("item") or {}
                    if observed.get("id") != row["item_id"]:
                        raise Conflict("Снимок относится к другому предмету сделки")
                    Orders(self.store).ingest(deal)
                    snapshot_orders[row["item_id"]] = row["id"]
                if observed.get("id") != row["item_id"] or (observed.get("user") or {}).get("id") != account:
                    raise Conflict("Владелец оплаченного объявления не подтверждён")
                paid_items[row["item_id"]] = deal
                if not any(item["id"] == row["item_id"] for item in snapshot):
                    snapshot.append(observed)
            except ValueError as error:
                code = "FULL_DEAL_VALIDATION_" + type(error).__name__.upper()
                if not self.store.rows(
                    "SELECT 1 FROM audit_log WHERE action='DEAL_BINDING_BLOCKED' AND target=? AND outcome=? LIMIT 1",
                    (row["id"], code),
                ):
                    self.store.audit(None, "DEAL_BINDING_BLOCKED", row["id"], code)
                    event(
                        "CHECK",
                        "Оплаченная сделка не прошла проверку кандидата на привязку · " + code,
                        order=row["id"],
                        level=10,
                    )
                continue
            except APIError as error:
                if error.code in RATE_LIMIT_CODES:
                    # Candidates are linked only after the whole batch is read.
                    self.store.set_setting(key + ":paid_cursor", batch_start)
                    raise
                self.store.audit(None, "DEAL_ITEM_READ", row["id"], error.code)
                event(
                    "WARNING", "Не удалось проверить товар сделки: " + error.code, order=row["id"], level=10
                )
                continue
        if len(pending_orders) < 20:
            self.store.set_setting(key + ":paid_cursor", "")
        adopted = 0
        for product in products:
            candidates = []
            valid_snapshot = []
            malformed = False
            for item in snapshot:
                if item.get("status") in MODERATION_STATUSES:
                    linked = self.service.listing(item["id"])
                    if not linked or linked["id"] != product["id"]:
                        continue
                try:
                    code = marker(item.get("name"))
                except ValueError:
                    linked = self.service.listing(item["id"])
                    if (
                        f"[{product['code']}]" in item["name"].upper()
                        or linked
                        and linked["id"] == product["id"]
                    ):
                        malformed = True
                    continue
                valid_snapshot.append(item)
                if code == product["code"]:
                    candidates.append(item)
            # A malformed code of this product can hide a duplicate marker.
            if malformed:
                self.problem(product, "CATALOG_MARKER_INVALID")
                continue
            if (
                sum(
                    item["id"] not in snapshot_orders and item.get("status") == "APPROVED"
                    for item in candidates
                )
                > 1
            ):
                self.problem(product, "DUPLICATE_ACTIVE_MARKER")
                continue
            if not candidates and product["error_code"] == "RELIST_CRITICAL_FIELDS_CHANGED":
                # A sale can leave the old listing outside the active catalogue.
                # Recheck only the exact public alias named by the saved incident.
                problem = self.store.setting("logical:problem:" + product["id"], {})
                linked = self.service.listing(problem.get("candidate_id"))
                if (
                    linked
                    and linked["id"] == product["id"]
                    and not linked["source_order_id"]
                    and self.service.same_item_problem(linked, "RELIST_CRITICAL_FIELDS_CHANGED")
                ):
                    try:
                        sold = await self.api.call("get_item", id=linked["item_id"])
                    except APIError as error:
                        if error.code != "HTTP_404":
                            raise
                    else:
                        if sold.get("id") == linked["item_id"] and sold.get("status") == "SOLD":
                            require_listing_details(sold)
                            candidates.append(sold)
            for item in candidates:
                if (
                    not product["inherit_enabled"]
                    and item["id"] not in snapshot_orders
                    and not self.service.listing(item["id"])
                ):
                    continue
                if product_id and not self.service.listing(item["id"]):
                    # An explicit binding recovery can verify existing aliases,
                    # never adopt a new item or enable an execution projection.
                    continue
                try:
                    # A separate fresh read confirms the selected object after
                    # the catalog scan; existing aliases are also rechecked.
                    source_order = snapshot_orders.get(item["id"])
                    if source_order:
                        current_deal = await self.api.call("get_delivery_deal", deal_id=source_order)
                        validate_candidate_order(current_deal, source_order)
                        compatible_detail(paid_items[item["id"]], current_deal)
                        fresh = current_deal.get("item") or {}
                        Orders(self.store).ingest(current_deal)
                        paid_items[item["id"]] = current_deal
                    else:
                        fresh = await self.api.call("get_item", id=item["id"])
                    require_listing_details(fresh)
                    if (
                        not source_order
                        and fresh.get("status") != item.get("status")
                        and {fresh.get("status"), item.get("status")} <= MODERATION_STATUSES | {"APPROVED"}
                    ):
                        # The next scan will use one coherent publication state.
                        continue
                    linked = self.service.listing(item["id"])
                    same_public_listing = (
                        linked
                        and linked["id"] == product["id"]
                        and not source_order
                        and not linked["source_order_id"]
                    )
                    sold_transition = (
                        linked
                        and linked["id"] == product["id"]
                        and not linked["source_order_id"]
                        and item.get("status") == "APPROVED"
                        and fresh.get("status") == "SOLD"
                    )
                    if (
                        fresh.get("id") != item["id"]
                        or fresh.get("status") != item.get("status")
                        and not sold_transition
                        or fresh.get("deleted_at") != item.get("deleted_at")
                        or fresh.get("description") != item.get("description")
                        and not same_public_listing
                        or critical(fresh) != critical(item)
                        and not same_public_listing
                    ):
                        raise RelistConflict("RELIST_ITEM_CHANGED")
                    await self.service.adopt(
                        product["owner_id"],
                        product["id"],
                        fresh,
                        valid_snapshot,
                        paid_order=paid_items.get(item["id"]),
                        source_order_id=source_order,
                        snapshot_item_ids=frozenset(snapshot_orders),
                        expected_product=product,
                    )
                except APIError as error:
                    if error.code in RATE_LIMIT_CODES and source_order:
                        self.store.set_setting(key + ":paid_cursor", batch_start)
                    self.store.set_setting(
                        key + ":error", {"code": error.code, "candidate_id": item["id"], "at": now()}
                    )
                    raise
                except (ValueError, Conflict) as error:
                    code = (
                        error.code
                        if isinstance(error, RelistConflict)
                        else "RELIST_BINDING_CONFLICT"
                        if isinstance(error, Conflict)
                        else "RELIST_CANDIDATE_INVALID"
                    )
                    self.problem(product, code, candidate_id=item["id"])
                    continue
                adopted += int(self.service.listing(item["id"]) is not None)
                if item["id"] in paid_items:
                    wake = getattr(self.store, "enrollment_wake", None)
                    if wake is not None:
                        wake.set()
                if source_order:
                    event("CHECK", "Привязка оплаченной сделки подтверждена", order=source_order)
        self.store.set_setting(
            key + ":completed", {"at": now(), "generation": generation, "adopted": adopted}
        )
        self.store.set_setting(key, {})
        return adopted

    def problem(self, product, code, *, candidate_id=None):
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE logical_products SET error_code=? WHERE id=? AND revision=? AND control_revision=? "
                "AND state=? AND enabled=? AND inherit_enabled=?",
                (
                    code,
                    product["id"],
                    product["revision"],
                    product["control_revision"],
                    product["state"],
                    product["enabled"],
                    product["inherit_enabled"],
                ),
            ).rowcount
            if not changed:
                return
            evidence = {
                "product_id": product["id"],
                "code": code,
                "candidate_id": candidate_id,
                "revision": product["revision"],
                "control_revision": product["control_revision"],
            }
            self.store.set_setting("logical:problem:" + product["id"], evidence)
            db.execute(
                "INSERT OR IGNORE INTO inbox(key,kind,payload,received_at,processed_at) VALUES(?,'logical_product_blocked',?,?,?)",
                (
                    "logical-problem:"
                    + product["id"]
                    + ":"
                    + str(product["revision"])
                    + ":"
                    + str(product["control_revision"])
                    + ":"
                    + code
                    + (":" + candidate_id if candidate_id else ""),
                    encode(evidence),
                    now(),
                    now(),
                ),
            )

    async def _scan_with_connection_check(self, stop):
        try:
            return await self.once()
        except APIError as error:
            connection = getattr(self.api, "connection", None)
            if error.code != "CONNECTION_NOT_VERIFIED" or connection is None or stop.is_set():
                raise
            if connection.state.get("reason") == "CHECK_IN_PROGRESS":
                try:
                    async with asyncio.timeout(5), connection.lock:
                        pass
                except TimeoutError:
                    raise error from None
            if connection.state.get("status") != "CONNECTED" or stop.is_set():
                raise
        return await self.once()

    async def run(self, stop):
        from app.workers.supervisor import worker_cycle

        failures = 0
        while not stop.is_set():
            self.paid_wake.clear()
            prior = self.store.setting("runtime:cycle:logical-products", {})
            try:
                with worker_cycle(
                    self.store, "logical-products", min(900, 30 * 2 ** min(failures + 1, 5))
                ) as cycle:
                    try:
                        await self._scan_with_connection_check(stop)
                    except APIError as error:
                        if error.code == "CONNECTION_NOT_VERIFIED":
                            cycle["retry_delay"] = 30
                        if str(error.retry_after or "").isdigit():
                            cycle["retry_delay"] = max(cycle["retry_delay"], int(error.retry_after))
                        raise
                if prior.get("consecutive_failures", 0) >= 3:
                    event("INFO", "Сверка перевыставлений восстановлена", level=10)
                failures = 0
            except (APIError, ValueError) as error:
                failures += 1
                event(
                    "WARNING",
                    "Не удалось завершить сверку перевыставлений: "
                    + getattr(error, "code", type(error).__name__),
                    level=10,
                )
                if prior.get("consecutive_failures", 0) == 2 and getattr(
                    error, "code", None
                ) not in RATE_LIMIT_CODES | {"CONNECTION_NOT_VERIFIED"}:
                    event("WARNING", "Перевыставления временно недоступны. Бот повторит проверку.", level=30)
            pending = self.store.setting("logical:scan:" + str(self.api.account.id), {})
            delay = cycle["retry_delay"] if failures else 5 if pending else 120
            if failures:
                try:
                    await asyncio.wait_for(stop.wait(), delay)
                except TimeoutError:
                    pass
                continue
            waiting = [asyncio.create_task(signal.wait()) for signal in (stop, self.paid_wake)]
            try:
                await asyncio.wait(waiting, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in waiting:
                    task.cancel()
                await asyncio.gather(*waiting, return_exceptions=True)
