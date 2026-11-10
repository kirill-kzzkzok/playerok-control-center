"""Real owner setup → PAID snapshot (public get_item=404) → existing gift workers."""

import copy

import pytest

from app.gifts.worker import GiftWorker
from app.playerok.adapter import APIError
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.workers.logical_products import LogicalProductWorker
from tests.test_logical_faults import reopen
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_universal_delivery_e2e import simulation as simulation_fixture


@pytest.fixture
async def case(tmp_path, request):
    async for create in simulation_fixture.__wrapped__(tmp_path):
        r = await create(gift=True)
        await setup_ui(r, "telegram_gifts", "gift: heart, heart")
        r.original = copy.deepcopy(r.api.item)
        r.api.item["status"] = "SOLD"
        order = r.api.order("snapshot-paid")
        order["item"]["id"] = "deal-item-copy"
        order["item"]["status"] = "APPROVED"
        r.full = copy.deepcopy(order)
        r.api.orders["snapshot-paid"] = order
        # List/persisted-deal data are partial like real Playerok. Only the new
        # read-only full contract supplies the authoritative fields and marker.
        r.partial = copy.deepcopy(order)
        r.partial["item"].update(description=None, attributes=None)
        original_call = r.api.call

        async def call(method, r=r, original_call=original_call, **kwargs):
            if method == "get_item" and kwargs["id"] == "deal-item-copy":
                raise APIError("HTTP_404")
            if method == "get_deal" and kwargs["deal_id"] == "snapshot-paid":
                return copy.deepcopy(r.partial)
            if method == "get_delivery_deal":
                assert kwargs == {"deal_id": "snapshot-paid"}
                return copy.deepcopy(r.full)
            return await original_call(method, **kwargs)

        r.api.call = call
        if getattr(request, "param", True):
            Orders(r.store).ingest(r.partial)
        yield r


async def test_paid_snapshot_404_binds_exact_order_and_bundle_survives_restart(case):
    r = case
    await LogicalProductWorker(r.store, r.api).once()
    link = LogicalProducts(r.store, r.api).listing("deal-item-copy")
    assert link is not None
    assert link["source_order_id"] == "snapshot-paid"
    assert link["handler"] == "telegram_gifts"
    assert not r.api.remote.payments
    gifts = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    await gifts.once()
    await r.worker.once()
    assert len(r.api.remote.payments) == 1
    reopen(r)
    await r.worker.once()
    assert len(r.api.remote.payments) == 2

    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
    assert not await r.worker.once()
    assert r.store.rows("SELECT status FROM orders WHERE id='snapshot-paid'") == [{"status": "PAID"}]
    # Same snapshot ID cannot authorize another sale, even with identical fields.
    other = {**copy.deepcopy(r.full), "id": "other-order"}
    Orders(r.store).ingest(other)
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    assert not r.store.rows("SELECT 1 FROM gift_jobs WHERE order_id='other-order'")
    assert len(r.api.remote.payments) == 2


async def test_real_partial_without_chat_is_hydrated_before_validation_and_sync_cannot_erase_chat(case):
    r = case
    r.partial["chat"] = None
    r.partial["item"]["category"] = None
    Orders(r.store).ingest(r.partial)
    await LogicalProductWorker(r.store, r.api).once()
    link = LogicalProducts(r.store, r.api).listing("deal-item-copy")
    assert link is not None, "An incomplete list deal must not stop the authoritative read"
    # A polling response may overwrite the detail right before gift enrollment/dispatch.
    Orders(r.store).ingest(r.partial)
    gifts = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    await gifts.once()
    assert r.store.rows("SELECT order_id FROM gift_jobs") == [{"order_id": "snapshot-paid"}]
    Orders(r.store).ingest(r.partial)
    await r.worker.once()
    Orders(r.store).ingest(r.partial)
    await r.worker.once()
    assert len(r.api.remote.payments) == 2
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
    assert not await r.worker.once()


async def test_manually_completed_partial_purchase_never_gets_catchup_gift(case):
    r = case
    r.partial.update(chat=None, status="CONFIRMED")
    r.full["status"] = "CONFIRMED"
    Orders(r.store).ingest(r.partial)
    await LogicalProductWorker(r.store, r.api).once()
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    assert not r.store.rows("SELECT * FROM gift_jobs")
    assert not r.api.remote.payments


async def test_known_gift_listing_also_hydrates_missing_chat_before_payment(case):
    r = case
    full = {**copy.deepcopy(r.full), "id": "known-paid", "item": copy.deepcopy(r.original)}
    partial = {**copy.deepcopy(full), "chat": None}
    partial["item"].update(description=None, category=None)
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_deal" and kwargs["deal_id"] == "known-paid":
            return copy.deepcopy(partial)
        if method == "get_delivery_deal" and kwargs["deal_id"] == "known-paid":
            return copy.deepcopy(full)
        return await original(method, **kwargs)

    r.api.call = call
    Orders(r.store).ingest(partial)
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    assert r.store.rows("SELECT order_id FROM gift_jobs") == [{"order_id": "known-paid"}]
    await r.worker.once()
    await r.worker.once()
    assert len(r.api.remote.payments) == 2


@pytest.mark.parametrize("fault", ["missing-chat", "other-order", "other-item", "other-owner"])
async def test_incomplete_paid_deal_never_uses_an_unconfirmed_full_response(case, fault):
    r = case
    r.partial["chat"] = None
    if fault == "missing-chat":
        r.full["chat"] = None
    elif fault == "other-order":
        r.full["id"] = "wrong-order"
    elif fault == "other-item":
        r.full["item"]["id"] = "wrong-item"
    else:
        r.full["item"]["user"]["id"] = "wrong-owner"
    Orders(r.store).ingest(r.partial)
    for _ in range(2):
        await LogicalProductWorker(r.store, r.api).once()
        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    assert not r.store.rows("SELECT * FROM gift_jobs")
    assert not r.api.remote.payments
    if fault == "missing-chat":
        assert LogicalProducts(r.store, r.api).listing(r.full["item"]["id"]) is None
        assert (
            r.store.rows(
                "SELECT outcome FROM audit_log WHERE action='DEAL_ITEM_READ' AND target='snapshot-paid'"
            )
            == [{"outcome": "DELIVERY_CHAT_NOT_YET_AVAILABLE"}] * 2
        )
        return
    assert (
        len(
            r.store.rows(
                "SELECT outcome FROM audit_log WHERE action='DEAL_BINDING_BLOCKED' AND target='snapshot-paid'"
            )
        )
        == 1
    )


@pytest.mark.parametrize("fault", ["closed", "owner", "price", "attributes", "description", "stopped"])
async def test_snapshot_conflicts_never_prepare_a_payment(case, fault):
    r = case
    if fault == "closed":
        r.full["status"] = "CONFIRMED"
    elif fault == "owner":
        r.full["item"]["user"]["id"] = "different-shop"
    elif fault == "price":
        r.full["item"]["price"] += 1
    elif fault == "attributes":
        r.full["item"]["attributes"] = {"amount": "unexpected"}
    elif fault == "description":
        r.full["item"]["description"] = "Other product\n#7654321"
    else:
        p = LogicalProducts(r.store, r.api).listing("lot")
        LogicalProducts(r.store, r.api).stop(7, p["id"])
    await LogicalProductWorker(r.store, r.api).once()
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    assert not r.store.rows("SELECT * FROM gift_jobs")
    assert not r.api.remote.payments


async def test_snapshot_unknown_payment_blocks_next_step_and_replay_after_restart(case):
    r = case
    await LogicalProductWorker(r.store, r.api).once()
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    r.api.remote.fail_at = 0
    await r.worker.once()
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "UNCERTAIN"}]
    assert len(r.api.remote.payments) == 1
    reopen(r)
    assert not await r.worker.once()
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    assert not await r.worker.once()
    assert len(r.api.remote.payments) == 1
    assert not r.store.rows("SELECT * FROM outbox WHERE kind='gift_refund'")


async def test_consistent_updated_sale_price_in_private_snapshot_keeps_gift_identity(case):
    r = case
    r.partial["item"]["price"] = r.full["item"]["price"] = 90
    await LogicalProductWorker(r.store, r.api).once()
    assert LogicalProducts(r.store, r.api).listing("deal-item-copy") is not None
    gifts = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    for _ in range(4):
        await gifts.once()
        await r.worker.once()
    assert len(r.api.remote.payments) == 2
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]


async def test_private_order_snapshot_is_not_offered_as_a_public_gift_product(case):
    from tests.test_ux_redesign import buttons

    r = case
    await LogicalProductWorker(r.store, r.api).once()
    await r.ui.click("gft:bindings")
    visible = buttons(r.session.last_panel)
    assert "gft:item:lot" in visible
    assert "gft:item:deal-item-copy" not in visible


async def test_unlinked_paid_snapshot_is_not_presented_as_intentional_manual_delivery(case):
    r = case
    # The normalized paid record can contain the marker before its alias scan.
    Orders(r.store).ingest(r.full)
    await r.ui.click("order:snapshot-paid")
    assert "Выдача: вручную" not in r.session.last_panel.text
    assert "сверк" in r.session.last_panel.text.lower()
    assert not r.api.remote.payments


@pytest.mark.parametrize("change", ["cancel", "stop"])
async def test_snapshot_rechecks_status_and_stop_before_next_gift(case, change):
    r = case
    await LogicalProductWorker(r.store, r.api).once()
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert len(r.api.remote.payments) == 1
    if change == "cancel":
        r.full["status"] = "ROLLED_BACK"
        r.partial["status"] = "ROLLED_BACK"
    else:
        product = LogicalProducts(r.store, r.api).listing("lot")
        LogicalProducts(r.store, r.api).stop(7, product["id"])
    await r.worker.once()
    assert len(r.api.remote.payments) == 1
    assert r.store.rows("SELECT state FROM gift_steps ORDER BY position")[0] == {"state": "SUCCEEDED"}
    assert not r.store.rows("SELECT * FROM outbox WHERE kind='gift_refund'")


async def test_full_delivery_read_uses_existing_safe_get_transport_without_mutations():
    import httpx

    from app.playerok.adapter import Playerok, SafeAccount
    from app.playerok.thread_calls import close_calls

    requests = []
    ident = "11111111-1111-4111-8111-111111111111"

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.params["operationName"] == "deal"
        query = request.url.params["query"]
        assert query.startswith("query deal(") and "stockType" in query
        assert "fragment RegularItem" in query and "attributes" in query
        assert ident in query
        return httpx.Response(
            200,
            json={
                "data": {
                    "deal": {
                        "id": ident,
                        "status": "PAID",
                        "direction": "OUT",
                        "hasProblem": False,
                        "isAutomated": False,
                        "chat": {"id": "chat"},
                        "item": {
                            "__typename": "MyItem",
                            "id": "copy",
                            "attributes": {"amount": 1},
                            "user": {"id": "seller"},
                            "obtainingType": {"id": "gift", "stockType": "UNLIMITED"},
                        },
                        "obtainingFields": [
                            {"id": "username-field", "value": "@Buyer", "type": "OBTAINING_DATA"}
                        ],
                    }
                }
            },
        )

    account = SafeAccount(
        "token=isolated-not-real",
        writes_enabled=False,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    api = Playerok(account)
    try:
        result = await api.call("get_delivery_deal", deal_id=ident)
        assert result["id"] == ident and result["status"] == "PAID"
        assert result["item"]["attributes"] == {"amount": 1}
        assert result["item"]["obtaining_type"]["stock_type"] is not None
        assert result["obtaining_fields"][0]["value"] == "@Buyer"
        assert len(requests) == 1
        with pytest.raises(APIError, match="INVALID_DEAL_ID"):
            await api.call("get_delivery_deal", deal_id="invalid-injection")
        assert len(requests) == 1
    finally:
        await close_calls(api)
        account.close()


async def test_catalog_sale_during_hydration_does_not_raise_false_binding_conflict(case):
    r = case
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_my_items":
            return r.api.page("items", [r.original])
        return await original(method, **kwargs)

    r.api.call = call
    await LogicalProductWorker(r.store, r.api).once()
    assert LogicalProducts(r.store, r.api).listing("lot")["server_status"] == "SOLD"
    assert not r.store.rows("SELECT 1 FROM inbox WHERE kind='logical_product_blocked'")
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    await r.worker.once()
    assert len(r.api.remote.payments) == 2


async def test_existing_false_sale_conflict_clears_without_delivering_manually_sent_order(case):
    r = case
    r.partial["status"] = r.full["status"] = "SENT"
    Orders(r.store).ingest(r.partial)
    logical = LogicalProducts(r.store, r.api)
    before = logical.listing("lot")
    worker = LogicalProductWorker(r.store, r.api)
    worker.problem(before, "RELIST_CRITICAL_FIELDS_CHANGED", candidate_id="lot")
    await worker.once()
    after = logical.listing("lot")
    assert after["error_code"] is None and after["server_status"] == "SOLD"
    assert after["enabled"] == before["enabled"] == 1
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    assert not r.store.rows("SELECT 1 FROM gift_jobs")
    assert not r.api.remote.payments


async def test_sold_source_with_changed_category_cannot_clear_saved_conflict(case):
    r = case
    r.partial["status"] = r.full["status"] = "SENT"
    Orders(r.store).ingest(r.partial)
    r.api.item["category"] = {"id": "different-category"}
    logical = LogicalProducts(r.store, r.api)
    worker = LogicalProductWorker(r.store, r.api)
    worker.problem(logical.listing("lot"), "RELIST_CRITICAL_FIELDS_CHANGED", candidate_id="lot")
    await worker.once()
    assert logical.listing("lot")["error_code"] is not None
    assert not r.store.rows("SELECT 1 FROM gift_jobs")
    assert not r.api.remote.payments


async def test_paid_private_snapshot_is_processed_when_public_inheritance_is_off(case):
    r = case
    r.store.execute("UPDATE logical_products SET inherit_enabled=0")
    logical = LogicalProducts(r.store, r.api)
    r.api.items["new-public"] = {**copy.deepcopy(r.original), "id": "new-public"}
    await LogicalProductWorker(r.store, r.api).once()
    assert logical.listing("new-public") is None
    link = logical.listing("deal-item-copy")
    assert link and link["source_order_id"] == "snapshot-paid"
    assert not link["inherit_enabled"]
    gifts = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    await gifts.once()
    await r.worker.once()
    await r.worker.once()
    assert len(r.api.remote.payments) == 2
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]


@pytest.mark.parametrize("case", [False], indirect=True)
async def test_new_paid_private_snapshot_wakes_idle_binding_worker(case):
    import asyncio

    r = case
    r.store.execute("UPDATE logical_products SET inherit_enabled=0")
    worker = LogicalProductWorker(r.store, r.api)
    initial_scan, ready, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
    once = worker.once

    async def observed_scan():
        await once()
        initial_scan.set()
        if LogicalProducts(r.store, r.api).listing("deal-item-copy"):
            ready.set()

    worker.once = observed_scan
    task = asyncio.create_task(worker.run(stop))
    try:
        await asyncio.wait_for(initial_scan.wait(), 3)
        assert not ready.is_set()
        Orders(r.store).ingest(r.partial)
        await asyncio.wait_for(ready.wait(), 3)
        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
        await r.worker.once()
        await r.worker.once()
        assert len(r.api.remote.payments) == 2
    finally:
        stop.set()
        await asyncio.wait_for(task, 3)


@pytest.mark.parametrize("case", [False], indirect=True)
async def test_paid_private_snapshot_missing_list_timestamp_reads_server_date(case):
    r = case
    r.partial["created_at"] = None
    Orders(r.store).ingest(r.partial)
    await LogicalProductWorker(r.store, r.api).once()
    link = LogicalProducts(r.store, r.api).listing("deal-item-copy")
    assert link and link["source_order_id"] == "snapshot-paid"
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    await r.worker.once()
    assert len(r.api.remote.payments) == 2


@pytest.mark.parametrize("case", [False], indirect=True)
async def test_paid_snapshot_timestamp_enrichment_wakes_once_without_new_paid_event(case):
    r = case
    worker = LogicalProductWorker(r.store, r.api)
    r.partial["created_at"] = None
    orders = Orders(r.store)
    assert orders.ingest(r.partial)
    worker.paid_wake.clear()
    assert not orders.ingest(r.full)
    assert worker.paid_wake.is_set()
    worker.paid_wake.clear()
    assert not orders.ingest(r.full)
    assert not worker.paid_wake.is_set()
