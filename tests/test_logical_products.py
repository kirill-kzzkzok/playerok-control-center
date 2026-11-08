"""Universal binding contracts; production services, isolated SQLite and API boundary."""

import asyncio
import copy
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.database.store import Conflict, Store
from app.services.logical_products import LogicalProducts, configuration, marked_title, marker
from app.services.products import Products
from app.workers.outbox import Executor, OutboxWorker


class ReadAPI:
    def __init__(self):
        self.account = SimpleNamespace(id="seller", writes_enabled=True, automation_enabled=True)
        self.item = {
            "id": "lot",
            "user": {"id": "seller"},
            "name": "Real digital product",
            "description": "Original description",
            "price": 100,
            "status": "APPROVED",
            "category": {"id": "category"},
            "obtaining_type": {"id": "obtaining"},
        }
        self.calls = []
        self.lock = asyncio.Lock()
        self.mutations = []
        self.lost_ack = False
        self.account.permit = lambda operation: nullcontext()
        self.account.update_item = self.update_item

    def update_item(self, id, **changes):
        assert id == self.item["id"]
        self.mutations.append(copy.deepcopy(changes))
        self.item.update(changes)
        if self.lost_ack:
            raise TimeoutError("external boundary lost ACK")
        return copy.deepcopy(self.item)

    async def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        assert method == "get_item"
        return copy.deepcopy(self.item)


@pytest.fixture
def logical(tmp_path):
    store = Store(tmp_path / "logical.sqlite3")
    store.execute("INSERT INTO administrators VALUES(7)")
    store.set_setting("playerok:account_id", "seller")
    api = ReadAPI()
    yield store, api, LogicalProducts(store, api)
    store.close()


def test_marker_is_exact_title_suffix_and_preserves_text():
    assert marker("Text [ab6]") == "AB6"
    assert marker("Description\n#1234567") is None
    assert marked_title("Text", "ab6") == "Text [AB6]"
    assert marked_title("Text [ab6]", "AB6") == "Text [AB6]"
    for value in ("[AB6] Text", "Text [AB6] [LF9]", "Text [AB!]", "Text [ABCD]"):
        with pytest.raises(ValueError):
            marker(value)
    with pytest.raises(ValueError):
        marked_title("Text [LF9]", "AB6")


def test_internal_gift_configuration_uses_existing_strict_parser():
    assert configuration("telegram_gifts", {"code": " Heart , HEART "})["code"] == "heart, heart"
    with pytest.raises(ValueError):
        configuration("telegram_gifts", {"code": "unknown"})


async def test_prepare_is_read_only_commit_queues_exact_description_and_stays_off(logical):
    store, api, service = logical
    prepared = await service.prepare(7, "lot", "fixed_text", {"text": "Delivery data", "after": "Thanks"})
    assert not store.rows("SELECT * FROM logical_products")
    assert not store.rows("SELECT * FROM outbox")
    result = service.commit(7, prepared)
    product = service.product(result["product_id"], 7)
    assert marker("Text [" + product["code"] + "]") == product["code"]
    assert product["enabled"] == 0
    assert product["state"] == "PENDING"
    op = store.rows("SELECT * FROM outbox")[0]
    assert op["kind"] == "edit"
    assert op["target"] == "lot"
    assert all(method == "get_item" for method, _ in api.calls)
    assert service.commit(7, prepared) == result


async def test_commit_rejects_account_change_and_disabled_writes(logical):
    store, api, service = logical
    prepared = await service.prepare(7, "lot", "manual", {})
    api.account.id = "other"
    with pytest.raises(ValueError):
        service.commit(7, prepared)
    assert not store.rows("SELECT * FROM logical_products")
    api.account.id = "seller"
    api.account.writes_enabled = False
    with pytest.raises(ValueError):
        service.commit(7, prepared)
    assert not store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("case", ["success", "changed", "wrong_item", "lost_ack"])
async def test_binding_runs_real_outbox_and_independent_readback(logical, case):
    store, api, service = logical
    prepared = await service.prepare(7, "lot", "manual", {})
    result = service.commit(7, prepared)
    if case == "changed":
        api.item["description"] = "Owner changed description"
    if case == "wrong_item":
        original = api.call

        async def wrong_once(method, **kwargs):
            result = await original(method, **kwargs)
            if len(api.calls) == 2:
                result["id"] = "different-lot"
            return result

        api.call = wrong_once
    api.lost_ack = case == "lost_ack"
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    op = store.rows("SELECT status FROM outbox")[0]["status"]
    product = service.product(result["product_id"])
    if case == "success":
        assert op == "SUCCEEDED"
        assert product["state"] == "VERIFIED"
        assert product["enabled"] == 0
        assert marker(api.item["name"]) == product["code"]
        assert len(api.calls) >= 3
    elif case in {"changed", "wrong_item"}:
        assert op == "PERMANENT_FAILED"
        assert not api.mutations
    else:
        assert op == "UNCERTAIN"
        assert len(api.mutations) == 1
        assert product["state"] == "PENDING"
        assert not await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
        api.account.writes_enabled = False
        await service.reconcile_binding(7, product["id"])
        assert service.product(product["id"])["state"] == "VERIFIED"
        assert store.rows("SELECT status FROM outbox")[0]["status"] == "SUCCEEDED"
        assert len(api.mutations) == 1
    assert product["enabled"] == 0


def test_legacy_migration_is_repeatable_and_does_not_activate_or_rewrite(logical):
    store, api, service = logical
    store.set_setting("gifts:listing_options:old", {"order": 30, "day": 70, "after": "Original thanks"})
    store.execute(
        "INSERT INTO gift_listings(item_id,owner_id,gift_code,gift_id,approved_stars,description_sha256,category_id,obtaining_type_id,username_field_id,bundle) "
        "VALUES('old',7,'heart','5170145012310081615',15,'oldhash','category','obtaining','username',?)",
        ('[{"code":"heart","gift_id":"5170145012310081615","stars":15}]',),
    )
    old = store.rows("SELECT * FROM gift_listings")
    assert service.migrate_legacy() == 1
    snapshot = store.rows("SELECT * FROM logical_products")
    import json

    assert json.loads(snapshot[0]["config"])["after"] == "Original thanks"
    assert snapshot[0]["state"] == "LEGACY" and snapshot[0]["enabled"] == 0
    assert service.migrate_legacy() == 0
    store.apply_migrations()
    assert store.rows("SELECT * FROM logical_products") == snapshot
    assert store.rows("SELECT * FROM gift_listings") == old
    assert not store.rows("SELECT * FROM outbox")
    assert not api.calls


async def test_fixed_text_arms_only_explicitly_and_preserves_cutoff_on_relist(logical):
    store, api, service = logical
    prepared = await service.prepare(7, "lot", "fixed_text", {"text": "Delivery", "after": "Thanks"})
    result = service.commit(7, prepared)
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    product = service.product(result["product_id"])
    await service.arm(7, product["id"], product["revision"], inherit=True)
    product = service.product(product["id"])
    assert product["enabled"] == 1
    cutoff = product["armed_at"]
    old = copy.deepcopy(api.item)
    old["status"] = "SOLD"
    new = copy.deepcopy(api.item)
    new["id"] = "relisted"
    await service.adopt(7, product["id"], new, [old, new])
    assert service.listing("relisted")["id"] == product["id"]
    assert (
        store.rows("SELECT updated_at FROM delivery_policies WHERE item_id='relisted'")[0]["updated_at"]
        == cutoff
    )
    assert not store.rows("SELECT * FROM logical_order_scopes")
    assert len(api.mutations) == 1  # Only the explicitly confirmed description edit.


async def test_relist_rejects_active_duplicate_but_accepts_confirmed_sale_price(logical):
    store, api, service = logical
    result = service.commit(7, await service.prepare(7, "lot", "fixed_text", {"text": "Delivery"}))
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    product = service.product(result["product_id"])
    await service.arm(7, product["id"], product["revision"], inherit=True)
    new = copy.deepcopy(api.item)
    new["id"] = "relisted"
    with pytest.raises(ValueError):
        await service.adopt(7, product["id"], new, [api.item, new])
    old = copy.deepcopy(api.item)
    old["status"] = "SOLD"
    new["price"] = 999
    await service.adopt(7, product["id"], new, [old, new])
    adopted = service.listing("relisted")
    assert adopted["id"] == product["id"]
    assert adopted["config"] == service.product(product["id"])["config"]
    assert adopted["armed_at"] == service.product(product["id"])["armed_at"]


async def test_relist_cannot_adopt_changed_name_without_matching_fresh_server_item(logical):
    store, api, service = logical
    result = service.commit(7, await service.prepare(7, "lot", "fixed_text", {"text": "Delivery"}))
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    product = service.product(result["product_id"])
    await service.arm(7, product["id"], product["revision"], inherit=True)
    old = {**copy.deepcopy(api.item), "status": "SOLD"}
    new = {**copy.deepcopy(api.item), "id": "relisted", "name": "Different " + api.item["name"]}
    with pytest.raises(ValueError, match="изменился во время фоновой проверки"):
        await service.adopt(7, product["id"], new, [old, new])
    assert not service.listing("relisted")


@pytest.mark.parametrize("views_change", [False, True])
async def test_catalog_worker_discovers_new_id_without_changing_delivery_engine(
    logical, views_change, caplog
):
    from app.workers.logical_products import LogicalProductWorker

    caplog.set_level("INFO", logger="pcc.shop")
    store, api, service = logical
    result = service.commit(7, await service.prepare(7, "lot", "fixed_text", {"text": "Delivery"}))
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    product = service.product(result["product_id"])
    await service.arm(7, product["id"], product["revision"], inherit=True)
    new = copy.deepcopy(api.item)
    new["id"] = "relisted"
    counter = 0

    async def catalog(method, **kwargs):
        nonlocal counter
        if method == "get_my_items":
            return {"items": [{"id": "relisted"}], "page_info": {"has_next_page": False}}
        assert method == "get_item" and kwargs["id"] == "relisted"
        counter += 1
        return {**new, "views_counter": counter if views_change else 1}

    api.call = catalog
    assert await LogicalProductWorker(store, api).once() == 1
    assert service.listing("relisted")["id"] == product["id"]
    await LogicalProductWorker(store, api).once()
    relist = [record.getMessage() for record in caplog.records if "лот перевыставлен" in record.getMessage()]
    assert relist == [f"🔄 [{product['code']}] — лот перевыставлен, выдача сохранена"]
    assert len(api.mutations) == 1


async def test_stop_during_arm_read_wins_over_late_enable(logical):
    store, api, service = logical
    result = service.commit(7, await service.prepare(7, "lot", "fixed_text", {"text": "Delivery"}))
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    product = service.product(result["product_id"])
    original = api.call

    async def stop_during_read(method, **kwargs):
        value = await original(method, **kwargs)
        service.stop(7, product["id"])
        return value

    api.call = stop_during_read
    with pytest.raises(ValueError):
        await service.arm(7, product["id"], product["revision"])
    assert service.product(product["id"])["enabled"] == 0
    assert not store.rows("SELECT 1 FROM delivery_policies WHERE json_extract(payload,'$.enabled')=1")


async def test_second_old_preview_cannot_replace_first_verified_binding(logical):
    store, api, service = logical
    first = await service.prepare(7, "lot", "fixed_text", {"text": "First"})
    stale = await service.prepare(7, "lot", "fixed_text", {"text": "Second"})
    result = service.commit(7, first)
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    product = service.product(result["product_id"])
    await service.arm(7, product["id"], product["revision"])
    before = store.rows("SELECT * FROM logical_listings")
    with pytest.raises(ValueError):
        service.commit(7, stale)
    assert store.rows("SELECT * FROM logical_listings") == before
    assert len(store.rows("SELECT * FROM logical_products")) == 1
    assert len(store.rows("SELECT * FROM outbox")) == 1


async def test_stock_binding_accepts_seller_data_value_rotation_but_not_buyer_fields(logical):
    store, api, service = logical
    api.item["data_fields"] = [
        {"id": "seller", "type": "ITEM_DATA", "value": "old"},
        {"id": "buyer", "type": "OBTAINING_DATA", "required": False},
    ]
    result = service.commit(7, await service.prepare(7, "lot", "unique_stock", {}))
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    product = service.product(result["product_id"])
    api.item["data_fields"][0]["value"] = "rotated"
    api.item["comment"] = "seller comment rotated"
    service.verify_listing(product, api.item)
    api.item["data_fields"][1]["required"] = True
    with pytest.raises(Conflict, match="поля получения"):
        service.verify_listing(product, api.item)


async def test_relist_scan_skips_detail_reads_for_unrelated_catalog_items(logical):
    from app.workers.logical_products import LogicalProductWorker

    store, api, service = logical
    result = service.commit(7, await service.prepare(7, "lot", "fixed_text", {"text": "Delivery"}))
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    product = service.product(result["product_id"])
    await service.arm(7, product["id"], product["revision"])
    candidate = {**api.item, "id": "relisted"}
    unrelated = [
        {"id": f"other-{i}", "name": f"Other listing {i}", "status": "APPROVED", "user": candidate["user"]}
        for i in range(50)
    ]
    reads = []

    async def catalog(method, **kwargs):
        if method == "get_my_items":
            offset = int(kwargs["after_cursor"] or 0)
            items = [*unrelated, candidate]
            following = offset + 24
            return {
                "items": items[offset:following],
                "page_info": {"has_next_page": following < len(items), "end_cursor": str(following)},
            }
        reads.append(kwargs["id"])
        return candidate if kwargs["id"] == "relisted" else {**candidate, **unrelated[int(kwargs["id"][6:])]}

    api.call = catalog
    worker = LogicalProductWorker(store, api)
    assert await worker.once() == 0
    assert not reads
    assert await worker.once() == 1
    assert service.listing("relisted")["id"] == product["id"]
    assert reads == ["relisted", "relisted"]


async def test_unlinked_paid_deal_requests_short_retry_until_flags_arrive(logical):
    from datetime import datetime, timedelta

    from app.services.orders import Orders
    from app.workers.logical_products import LogicalProductWorker

    store, api, service = logical
    result = service.commit(7, await service.prepare(7, "lot", "fixed_text", {"text": "Delivery"}))
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    product = service.product(result["product_id"])
    await service.arm(7, product["id"], product["revision"])
    product = service.product(product["id"])
    candidate = {**api.item, "id": "relisted"}
    order = {
        "id": "paid-new",
        "status": "PAID",
        "direction": "OUT",
        "item": candidate,
        "user": {"id": "buyer"},
        "chat": {"id": "chat"},
        "created_at": (datetime.fromisoformat(product["armed_at"]) + timedelta(seconds=2)).isoformat(),
        "has_problem": None,
        "is_automated": False,
    }
    Orders(store).ingest(order)

    async def read(method, **kwargs):
        if method == "get_my_items":
            return {"items": [], "page_info": {"has_next_page": False}}
        if method in {"get_deal", "get_delivery_deal"}:
            return copy.deepcopy(order)
        assert method == "get_item" and kwargs["id"] == "relisted"
        return copy.deepcopy(candidate)

    api.call = read
    worker = LogicalProductWorker(store, api)
    assert await worker.once() == 0
    assert worker.paid_retry
    assert not service.listing("relisted")
    order["has_problem"] = False
    assert await worker.once() == 1
    assert service.listing("relisted")["id"] == product["id"]
    assert not worker.paid_retry
    assert len(api.mutations) == 1  # Only the original test title binding.
