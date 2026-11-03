"""Bounded read-only catalog reconciliation before existing delivery enrollment."""

import asyncio
import json
import uuid
from datetime import UTC, datetime

from app.database.store import Conflict, encode, now
from app.playerok.adapter import APIError
from app.security.logging import event
from app.services.automation import future_order
from app.services.logical_products import LogicalProducts, critical, marker
from app.services.order_management import deal_details, deliverable
from app.services.orders import Orders
from app.workers.pagination import PageScanner
from playerokapi.enums import ItemStatuses


class LogicalProductWorker:
    def __init__(self, store, api):
        self.store, self.api = store, api
        self.service = LogicalProducts(store, api)
        if not hasattr(store, "logical_scan_lock"):
            store.logical_scan_lock = asyncio.Lock()

    async def once(self):
        return await self.scan()

    async def scan(self, *, product_id=None):
        async with self.store.logical_scan_lock:
            return await self._scan(product_id=product_id)

    async def _scan(self, *, product_id=None):
        products = self.store.rows(
            "SELECT * FROM logical_products WHERE state='VERIFIED' AND "
            + ("id=?" if product_id else "enabled=1 AND inherit_enabled=1"),
            (product_id,) if product_id else (),
        )
        if not products:
            return 0
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
                statuses=[ItemStatuses.APPROVED],
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

        def validate_candidate_order(deal, ident):
            if deal.get("is_automated") is not True:
                return deliverable(deal, ident, account)
            from app.services.platform_orders import platform_sale

            code = marker((deal.get("item") or {}).get("name"))
            candidates = [
                p
                for p in products
                if json.loads(p["config"]).get("platform_only") is True
                and p["code"] == code
                and future_order(deal.get("created_at"), p["armed_at"])
            ]
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
            if not any(future_order(local.get("created_at"), p["armed_at"]) for p in products):
                continue
            try:
                deal = await deal_details(self.api, row["id"], full_item=True)
                validate_candidate_order(deal, row["id"])
                if (deal.get("item") or {}).get("id") != row["item_id"]:
                    raise Conflict("Предмет сделки изменился")
                try:
                    observed = await self.api.call("get_item", id=row["item_id"])
                except APIError as error:
                    if error.code != "HTTP_404":
                        raise
                    # Playerok deal.item can be a purchased snapshot whose ID
                    # is not a public item route. Read its full, exact deal.
                    deal = await self.api.call("get_delivery_deal", deal_id=row["id"])
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
                        "WARNING",
                        "Не удалось подтвердить полную карточку покупки; выдача не запускалась · " + code,
                        order=row["id"],
                    )
                continue
            except APIError as error:
                self.store.audit(None, "DEAL_ITEM_READ", row["id"], error.code)
                event(
                    "WARNING", "Не удалось проверить товар сделки: " + error.code, order=row["id"], level=30
                )
                continue
        if len(pending_orders) < 20:
            self.store.set_setting(key + ":paid_cursor", "")
        adopted = 0
        for product in products:
            candidates = []
            malformed = False
            for item in snapshot:
                try:
                    code = marker(item.get("name"))
                except ValueError:
                    linked = self.service.listing(item["id"])
                    if (
                        product["code"] in (item.get("description") or "")
                        or linked
                        and linked["id"] == product["id"]
                    ):
                        malformed = True
                    continue
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
            for item in candidates:
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
                        fresh = current_deal.get("item") or {}
                        Orders(self.store).ingest(current_deal)
                        paid_items[item["id"]] = current_deal
                    else:
                        fresh = await self.api.call("get_item", id=item["id"])
                    if (
                        fresh.get("id") != item["id"]
                        or fresh.get("status") != item.get("status")
                        or fresh.get("deleted_at") != item.get("deleted_at")
                        or fresh.get("description") != item.get("description")
                        or critical(fresh) != critical(item)
                    ):
                        raise Conflict("Объявление изменилось после загрузки каталога")
                    await self.service.adopt(
                        product["owner_id"],
                        product["id"],
                        fresh,
                        snapshot,
                        paid_order=paid_items.get(item["id"]),
                        source_order_id=source_order,
                        snapshot_item_ids=frozenset(snapshot_orders),
                        expected_product=product,
                    )
                except (ValueError, Conflict):
                    self.problem(product, "RELIST_CONFIRMATION_REQUIRED")
                    continue
                adopted += int(self.service.listing(item["id"]) is not None)
                if source_order:
                    event("ORDER", "Привязка сделки подтверждена: Telegram-подарки", order=source_order)
        self.store.set_setting(
            key + ":completed", {"at": now(), "generation": generation, "adopted": adopted}
        )
        self.store.set_setting(key, {})
        return adopted

    def problem(self, product, code):
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
                    + code,
                    encode({"product_id": product["id"], "code": code}),
                    now(),
                    now(),
                ),
            )

    async def run(self, stop):
        failures = 0
        while not stop.is_set():
            try:
                await self.once()
                failures = 0
            except (APIError, ValueError) as error:
                failures += 1
                event(
                    "WARNING",
                    "Сверка перевыставлений остановлена: " + getattr(error, "code", type(error).__name__),
                    level=30,
                )
            try:
                pending = self.store.setting("logical:scan:" + str(self.api.account.id), {})
                delay = min(900, 30 * 2 ** min(failures, 5)) if failures else 5 if pending else 120
                await asyncio.wait_for(stop.wait(), delay)
            except TimeoutError:
                pass
