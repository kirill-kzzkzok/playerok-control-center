import pytest

from app.database.store import Store, encode, now
from app.services.code_migration import CodeMigration
from app.services.logical_products import LogicalProducts, critical, digest
from app.services.products import Products
from app.workers.outbox import Executor, OutboxWorker
from tests.test_logical_products import ReadAPI


@pytest.fixture
def legacy(tmp_path):
    store = Store(tmp_path / "migration.sqlite")
    store.claim_first_owner(7)
    store.set_setting("playerok:account_id", "seller")
    api = ReadAPI()
    api.item.update(description="Description #steam\n#2163148", is_editable=True, priority="DEFAULT")
    stamp = now()
    config = encode({"text": "private unit", "after": "Thanks"})
    store.execute(
        "INSERT INTO logical_products(id,code,owner_id,account_id,handler,config,state,stock_item_id,baseline,original_description,enabled,inherit_enabled,armed_at,created_at,updated_at) VALUES('stable','#2163148',7,'seller','fixed_text',?,'VERIFIED','lot',?,?,1,1,?,?,?)",
        (config, encode(critical(api.item)), api.item["description"], stamp, stamp, stamp),
    )
    store.execute(
        "INSERT INTO logical_listings(item_id,product_id,config_revision,description_sha256,server_status,verified_at) VALUES('lot','stable',1,?,'APPROVED',?)",
        (digest(api.item["description"]), stamp),
    )
    yield store, api, CodeMigration(store, api)
    store.close()


@pytest.mark.parametrize("lost", [False, True])
async def test_migrate_through_guarded_outbox_then_readonly_recovery_without_replay(legacy, lost):
    store, api, service = legacy
    before = store.rows("SELECT * FROM logical_products")[0]
    plan = await service.prepare(7)
    assert plan["products"][0]["ready"], plan
    assert not store.rows("SELECT * FROM outbox")
    ids = service.queue(7, plan)
    assert len(ids) == 1
    assert service.queue(7, plan) == []
    assert store.rows("SELECT state FROM logical_products") == [{"state": "BLOCKED"}]
    worker = OutboxWorker(store, Executor(store, api, Products(store, api)))
    api.lost_ack = lost
    await worker.once()
    if lost:
        assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
        assert not await worker.once()
        await service.reconcile(7, "lot")
    row = store.rows("SELECT * FROM logical_products")[0]
    assert row["id"] == before["id"] and len(row["code"]) == 3
    assert api.item["name"].endswith("[" + row["code"] + "]")
    assert api.item["description"] == "Description #steam\n"
    for field in ("enabled", "inherit_enabled", "armed_at", "config", "revision", "stock_item_id"):
        assert row[field] == before[field]
    assert row["state"] == "VERIFIED"
    LogicalProducts(store, api).verify_listing(row, api.item)
    assert len(api.mutations) == 1
    assert not (await service.prepare(7))["products"]
    assert not store.rows("SELECT * FROM gift_jobs") and not store.rows("SELECT * FROM reservations")


async def test_automatic_run_is_scoped_and_reentrant_after_lost_response(legacy):
    store, api, service = legacy
    unrelated = store.enqueue("never-run", "reply", "buyer", {"text": "do not send"})
    api.lost_ack = True
    report = await service.run(7)
    assert report["verified"] == 1
    assert store.rows("SELECT status FROM outbox WHERE id=?", (unrelated,)) == [{"status": "PENDING"}]
    assert len(api.mutations) == 1
    assert (await service.run(7))["verified"] == 1
    assert len(api.mutations) == 1


@pytest.mark.parametrize("fault", ["not_editable", "changed", "owner", "uncertain"])
async def test_migration_refuses_unconfirmed_or_changed_context(legacy, fault):
    store, api, service = legacy
    if fault == "not_editable":
        api.item["is_editable"] = False
        assert not (await service.prepare(7))["products"][0]["ready"]
        return
    plan = await service.prepare(7)
    if fault == "owner":
        api.account.id = "other"
    if fault == "uncertain":
        ident = store.enqueue("unknown", "reply", "lot", {})
        op = store.claim()
        store.finish(ident, op["lease_token"], "UNCERTAIN")
    if fault in ("owner", "uncertain"):
        with pytest.raises(ValueError):
            service.queue(7, plan)
        assert not api.mutations
        return
    service.queue(7, plan)
    api.item["description"] += " changed outside"
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    assert not api.mutations
    assert store.rows("SELECT status FROM outbox") == [{"status": "PERMANENT_FAILED"}]


async def test_one_product_two_items_serial_claim_and_unknown_blocks_other_edit(legacy):
    import copy

    store, api, service = legacy
    second = copy.deepcopy(api.item)
    second["id"] = "archive"
    second["status"] = "SOLD"
    items = {"lot": api.item, "archive": second}

    async def call(method, **kwargs):
        assert method == "get_item"
        return copy.deepcopy(items[kwargs["id"]])

    writes = []

    def update(id, **changes):
        writes.append(id)
        items[id].update(changes)
        if len(writes) == 1:
            raise TimeoutError("lost result")
        return copy.deepcopy(items[id])

    api.call = call
    api.account.update_item = update
    link = store.rows("SELECT * FROM logical_listings")[0]
    store.execute(
        "INSERT INTO logical_listings(item_id,product_id,config_revision,description_sha256,server_status,verified_at) VALUES('archive','stable',1,?,'SOLD',?)",
        (link["description_sha256"], link["verified_at"]),
    )
    operations = service.queue(7, await service.prepare(7))
    assert len(operations) == 2
    first = store.claim()
    assert store.claim() is None
    # Restore only this proven-unstarted claim to exercise the production worker.
    store.execute(
        "UPDATE outbox SET status='PENDING',lease_token=NULL,lease_until=NULL WHERE id=? AND started_at IS NULL",
        (first["id"],),
    )
    worker = OutboxWorker(store, Executor(store, api, Products(store, api)))
    await worker.once()
    assert store.claim() is None
    unknown = store.rows("SELECT target FROM outbox WHERE status='UNCERTAIN'")[0]["target"]
    await service.reconcile(7, unknown)
    await worker.once()
    assert sorted(writes) == ["archive", "lot"]
    assert store.rows("SELECT state FROM logical_code_migrations") == [{"state": "VERIFIED"}]
    assert not store.rows("SELECT * FROM logical_order_scopes")


async def test_owner_dispatcher_bulk_preview_confirmation_restart_and_result(legacy):
    from aiogram import Bot

    from tests.test_telegram_integration import Interaction, TelegramSession
    from tests.test_telegram_transport import production_dispatcher

    store, api, _service = legacy
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    ui = Interaction(bot, production_dispatcher(store, api))
    try:
        await ui.click("codes:home")
        assert "Перенос кодов" in bot.session.last_panel.text
        await ui.click("codes:prepare")
        assert len(api.mutations) == 1
        nonce = "obsolete"
        ui.dp = production_dispatcher(store, api)
        await ui.click("codes:commit:" + nonce)
        await ui.click("codes:commit:" + nonce)
        assert len(store.rows("SELECT * FROM outbox")) == 1
        await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
        await ui.click("codes:results:0")
        assert "Подтверждено" in bot.session.last_panel.text
        assert len(api.mutations) == 1
        before = api.item["name"]
        await ui.click("codes:prepare", owner=8)
        assert api.item["name"] == before and len(api.mutations) == 1
        assert not store.rows("SELECT * FROM gift_jobs")
    finally:
        await bot.session.close()


async def test_late_old_paid_event_cannot_enroll_after_identity_change_but_new_sale_can(legacy):
    import copy
    from dataclasses import asdict

    from app.services.automation import DeliveryPolicy
    from app.services.orders import Orders
    from app.workers.enrollment import EnrollmentWorker

    store, api, service = legacy
    before = now()
    policy = DeliveryPolicy(enabled=True, mode="text", text="private unit", auto_enroll=True)
    store.execute("INSERT INTO delivery_policies VALUES('lot',1,7,?,?)", (encode(asdict(policy)), before))
    store.set_setting("delivery:lot:enabled", True)
    store.set_setting("orders:last_complete_sync", now())
    old = {
        "id": "late-old",
        "status": "PAID",
        "direction": "OUT",
        "has_problem": False,
        "is_automated": False,
        "created_at": before,
        "item": copy.deepcopy(api.item),
        "chat": {"id": "chat"},
    }
    service.queue(7, await service.prepare(7))
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    orders = {"late-old": old}
    original = api.call

    async def call(method, **kwargs):
        if method == "get_deal":
            return copy.deepcopy(orders[kwargs["deal_id"]])
        if method == "get_chat":
            return {"id": "chat", "is_texting_allowed": True}
        return await original(method, **kwargs)

    api.call = call
    Orders(store).ingest(old)
    await EnrollmentWorker(store, api).once()
    assert not store.rows("SELECT * FROM logical_order_scopes")
    assert not store.rows("SELECT * FROM delivery_chains")
    assert store.rows("SELECT status FROM auto_enrollment WHERE order_id='late-old'") == [
        {"status": "BLOCKED"}
    ]
    new = {**old, "id": "new-sale", "created_at": now(), "item": copy.deepcopy(api.item)}
    orders["new-sale"] = new
    Orders(store).ingest(new)
    await EnrollmentWorker(store, api).once()
    assert store.rows("SELECT order_id FROM delivery_chains") == [{"order_id": "new-sale"}]
    assert not store.rows("SELECT 1 FROM outbox WHERE kind='delivery_text'")
    assert len(api.mutations) == 1


async def test_historical_unknown_description_binding_can_only_be_resolved_readonly(legacy):
    store, api, _service = legacy
    store.execute("UPDATE logical_products SET state='PENDING',enabled=0 WHERE id='stable'")
    payload = {
        "description": api.item["description"],
        "logical_binding": {
            "product_id": "stable",
            "revision": 1,
            "account": "seller",
            "baseline": critical(api.item),
            "original_hash": digest("Original description"),
        },
    }
    op = store.enqueue("historical-binding", "edit", "lot", payload, actor=7)
    action = store.claim()
    store.mark_started(action)
    store.finish(op, action["lease_token"], "UNCERTAIN")
    store.execute("UPDATE logical_listings SET operation_id=?,verified_at=NULL WHERE item_id='lot'", (op,))
    api.account.writes_enabled = False
    logical = LogicalProducts(store, api)
    await logical.reconcile_binding(7, "stable")
    assert store.rows("SELECT status FROM outbox") == [{"status": "SUCCEEDED"}]
    assert not api.mutations
    assert logical.product("stable")["code"] == "#2163148"
    with pytest.raises(ValueError):
        logical.require_delivery("lot", "fixed_text")


async def test_stop_after_unknown_edit_allows_positive_readonly_resolution_without_enable(legacy):
    store, api, service = legacy
    service.queue(7, await service.prepare(7))
    api.lost_ack = True
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
    LogicalProducts(store, api).stop(7, "stable")
    api.account.writes_enabled = False
    await service.reconcile(7, "lot")
    assert store.rows("SELECT enabled,state FROM logical_products") == [{"enabled": 0, "state": "VERIFIED"}]
    assert len(api.mutations) == 1


async def test_catalog_discovers_owned_relisted_alias_but_not_unrelated_listing(legacy):
    import copy

    store, api, service = legacy
    alias = {**copy.deepcopy(api.item), "id": "new-alias"}
    ordinary = {**copy.deepcopy(api.item), "id": "ordinary", "description": "User text #steam"}
    items = {"lot": api.item, "new-alias": alias, "ordinary": ordinary}

    async def call(method, **kwargs):
        if method == "get_my_items":
            return {"items": list(items.values()), "page_info": {"has_next_page": False}}
        assert method == "get_item"
        return copy.deepcopy(items[kwargs["id"]])

    api.call = call
    assert await service.discover(7) == []
    assert service.logical.listing("new-alias")["id"] == "stable"
    assert not service.logical.listing("ordinary")
    assert await service.discover(7) == []
    assert len(store.rows("SELECT * FROM logical_listings")) == 2
    assert not api.mutations and not store.rows("SELECT * FROM outbox")


async def test_automatic_migration_continues_independent_product_after_block(legacy):
    import copy

    from app.services.logical_products import critical, digest

    store, api, service = legacy
    good = {**copy.deepcopy(api.item), "id": "good", "description": "Good description\n#7654321"}
    store.execute(
        "INSERT INTO logical_products SELECT 'second','#7654321',owner_id,account_id,handler,config,revision,control_revision,enabled,inherit_enabled,state,armed_at,'good',baseline,original_description,error_code,created_at,updated_at FROM logical_products WHERE id='stable'"
    )
    store.execute("UPDATE logical_products SET baseline=? WHERE id='second'", (encode(critical(good)),))
    store.execute(
        "INSERT INTO logical_listings(item_id,product_id,config_revision,description_sha256,server_status,verified_at) VALUES('good','second',1,?,'APPROVED',?)",
        (digest(good["description"]), now()),
    )
    api.item["is_editable"] = False
    items = {"lot": api.item, "good": good}

    async def call(method, **kwargs):
        assert method == "get_item"
        return copy.deepcopy(items[kwargs["id"]])

    def update(id, **changes):
        api.mutations.append(id)
        items[id].update(changes)
        return copy.deepcopy(items[id])

    api.call, api.account.update_item = call, update
    report = await service.run(7)
    assert report["verified"] == 1 and report["problems"], report
    assert api.mutations == ["good"]
    assert service.logical.product("stable")["code"] == "#2163148"
    assert service.logical.product("second")["state"] == "VERIFIED"
