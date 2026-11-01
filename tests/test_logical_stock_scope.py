"""Shared stock bucket contracts; real services/SQLite, test-only API boundary."""

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.database.store import Conflict, Store, encode, now
from app.services.automation import Automation, DeliveryPolicy
from app.services.inventory import Inventory
from app.services.orders import Orders
from app.services.reconciliation import Reconciliation
from app.telegram.stock_panel import StockPanel


@pytest.fixture
def stock(tmp_path):
    store = Store(tmp_path / "logical-stock.sqlite3")
    store.claim_first_owner(7)
    store.set_setting("playerok:account_id", "seller")
    armed = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    config = encode({"after": "Thanks"})
    store.execute(
        "INSERT INTO logical_products(id,code,owner_id,account_id,handler,config,state,enabled,armed_at,stock_item_id,baseline,original_description,created_at,updated_at) "
        "VALUES('product','#1234567',7,'seller','unique_stock',?,'VERIFIED',1,?,'original','{}','Description',?,?)",
        (config, armed, armed, armed),
    )
    policy = asdict(DeliveryPolicy(enabled=True, mode="stock", auto_enroll=True))
    for ident in ("original", "relisted"):
        store.execute(
            "INSERT INTO logical_listings(item_id,product_id,config_revision,description_sha256,server_status,verified_at) VALUES(?,'product',1,'hash','APPROVED',?)",
            (ident, now()),
        )
        store.execute("INSERT INTO delivery_policies VALUES(?,1,7,?,?)", (ident, encode(policy), armed))
    orders = {}

    async def call(method, **kwargs):
        if method == "get_item":
            return {
                "id": kwargs["id"],
                "name": "Stock product",
                "user": {"id": "seller"},
                "status": "APPROVED",
            }
        if method == "get_deal":
            return orders[kwargs["deal_id"]]
        if method == "get_chat":
            return {"id": kwargs["chat_id"], "is_texting_allowed": True}
        if method == "get_chat_messages":
            return {
                "messages": [
                    {"id": "receipt", "text": "unit-one", "user": {"id": "seller"}, "created_at": now()}
                ],
                "page_info": {"has_next_page": False},
            }
        raise AssertionError(method)

    api = SimpleNamespace(
        account=SimpleNamespace(id="seller", writes_enabled=True, automation_enabled=True), call=call
    )

    def order(ident, item="relisted", created=None):
        value = {
            "id": ident,
            "status": "PAID",
            "direction": "OUT",
            "has_problem": False,
            "is_automated": False,
            "item": {"id": item, "user": {"id": "seller"}},
            "chat": {"id": "chat-" + ident},
            "created_at": created or now(),
        }
        orders[ident] = value
        Orders(store).ingest(value)
        return value

    yield store, api, order
    store.close()


async def test_alias_import_counts_and_listing_use_one_bucket(stock):
    store, api, _ = stock
    Orders(store).add_stock("original", ["unit-one"])
    service = Inventory(store, api)
    assert service.counts("relisted")["AVAILABLE"] == 1
    preview = service.preview("relisted", "unit-one\nunit-two")
    assert preview["known"] == 1 and preview["new"] == 1
    assert await service.import_values(7, "relisted", "unit-one\nunit-two", preview, "APPROVED") == 1
    assert {r["item_id"] for r in store.rows("SELECT item_id FROM inventory")} == {"original"}
    state = SimpleNamespace()
    text, _ = await StockPanel(store, api).callback("stock:list:relisted:AVAILABLE:0", 7, state)
    assert "Единица №" in text and "unit-one" not in text
    assert service.counts("original")["AVAILABLE"] == 2


async def test_import_rejects_bucket_changed_after_quantity_confirmation(stock):
    store, api, _ = stock
    service = Inventory(store, api)
    preview = service.preview("relisted", "unit-one")
    # Same quantity is not proof that the owner confirmed the newly selected pool.
    store.execute("UPDATE logical_products SET stock_item_id='different-pool' WHERE id='product'")
    with pytest.raises(Conflict):
        await service.import_values(7, "relisted", "unit-one", preview, "APPROVED")
    assert not store.rows("SELECT * FROM inventory")


def test_alias_reservation_is_pinned_and_exhaustion_stops_all_aliases(stock):
    store, api, order = stock
    Orders(store).add_stock("original", ["unit-one"])
    order("sale")
    reserved = Orders(store).reserve("sale", actor=7)
    assert reserved is not None
    scope = store.rows("SELECT * FROM logical_order_scopes WHERE order_id='sale'")[0]
    assert scope["item_id"] == "relisted" and scope["stock_item_id"] == "original"
    assert store.rows("SELECT status FROM inventory WHERE id=?", (reserved,))[0]["status"] == "RESERVED"
    assert store.setting("delivery:original:enabled") is False
    assert store.setting("delivery:relisted:enabled") is False
    # Exhaustion stops admission, not the already reserved order's authority.
    from app.services.logical_products import LogicalProducts

    LogicalProducts(store, api).require_delivery("relisted", "unique_stock", "sale")
    assert Orders(store).reserve("sale", actor=7) == reserved


async def test_alias_orders_reserve_different_units_and_never_old_orders(stock):
    store, _api, order = stock
    Orders(store).add_stock("original", ["unit-one", "unit-two"])
    order("first", "original")
    order("second", "relisted")
    values = await asyncio.gather(
        asyncio.to_thread(Orders(store).reserve, "first", 7),
        asyncio.to_thread(Orders(store).reserve, "second", 7),
    )
    assert None not in values and len(set(values)) == 2
    assert len(store.rows("SELECT * FROM reservations")) == 2
    order("old", created=(datetime.now(UTC) - timedelta(days=1)).isoformat())
    with pytest.raises(Conflict):
        Orders(store).reserve("old", actor=7)
    assert not store.rows("SELECT * FROM logical_order_scopes WHERE order_id='old'")


async def test_readiness_and_simulation_see_shared_stock(stock):
    store, api, order = stock
    Orders(store).add_stock("original", ["unit-one"])
    order("sale")
    assert Automation(store, api).readiness("relisted")["counts"]["AVAILABLE"] == 1
    result = await Automation(store, api).simulate(7, "sale")
    assert result["available"] == 1 and result["would_deliver"]
    assert not store.rows("SELECT * FROM reservations")


async def test_uncertain_shared_reservation_can_be_verified_without_resending(stock):
    store, api, order = stock
    Orders(store).add_stock("original", ["unit-one"])
    order("sale")
    assert Orders(store).reserve("sale", actor=7)
    action = store.claim()
    store.finish(action["id"], action["lease_token"], "UNCERTAIN", error="LOST_ACK")
    evidence = await Reconciliation(store, api).evidence(7, action["id"], "receipt")
    assert evidence["decision"] == "CONFIRM_MESSAGE"
    assert store.rows("SELECT status FROM inventory")[0]["status"] == "QUARANTINED"
    assert len(store.rows("SELECT * FROM outbox")) == 1
    # A changed item ID cannot borrow the same pinned scope.
    store.execute("UPDATE orders SET item_id='unrelated' WHERE id='sale'")
    original = api.call

    async def changed(method, **kwargs):
        value = await original(method, **kwargs)
        if method == "get_deal":
            return {**value, "item": {**value["item"], "id": "unrelated"}}
        return value

    api.call = changed
    with pytest.raises(Conflict):
        await Reconciliation(store, api).evidence(7, action["id"], "receipt")
