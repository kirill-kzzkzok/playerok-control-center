"""Production Dispatcher → SQLite → enrollment/chains/Outbox; API boundary only."""

import asyncio
import threading
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from app.playerok.adapter import APIError
from app.services.automation import Automation
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.services.reconciliation import Reconciliation
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.logical_products import LogicalProductWorker
from tests.test_telegram_transport import production_dispatcher
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_universal_delivery_e2e import simulation as simulation_fixture
from tests.test_ux_redesign import buttons


@pytest.fixture
async def shop(tmp_path):
    async for factory in simulation_fixture.__wrapped__(tmp_path):

        async def create(factory=factory, **kwargs):
            r = await factory(**kwargs)

            def update_item(id, **changes):
                r.api.mutations.append(deepcopy(changes))
                r.api.items[id].update(changes)
                return deepcopy(r.api.items[id])

            r.api.account.update_item = update_item
            return r

        yield create


async def configure(r, reply=True, fulfill=True, text=None, item="lot"):
    await r.ui.click("iv:delivery:" + item)
    if "platform:item:" + item not in buttons(r.session.last_panel):
        await click(r, action="lp:method:" + item)
    assert "platform:item:" + item in buttons(r.session.last_panel)
    await click(r, action="platform:item:" + item)
    assert "Товар уже выдаётся самой площадкой" in r.session.last_panel.text
    if reply:
        await click(r, suffix=":reply")
    if fulfill:
        await click(r, suffix=":fulfill")
    if text:
        await click(r, suffix=":text")
        await r.ui.message(text)
    await click(r, suffix=":save")
    await finish_save(r, item)


async def finish_save(r, item="lot"):
    if any(k.endswith(":switch") for k in buttons(r.session.last_panel)):
        await click(r, suffix=":switch")
    if any(k.endswith(":bind") for k in buttons(r.session.last_panel)):
        await click(r, suffix=":bind")
        await r.worker.once()
        await r.ui.click("platform:item:" + item)
    assert "Привязка подтверждена" in r.session.last_panel.text


def purchase(r, ident="order", status="PAID", item="lot"):
    value = r.api.order(ident, item, status)
    value["is_automated"] = True
    Orders(r.store).ingest(value)
    return value


async def drain(r):
    for _ in range(12):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        while await r.worker.once():
            pass


@pytest.mark.parametrize("reply,fulfill", [(True, True), (True, False), (False, True), (False, False)])
async def test_independent_settings_and_future_sale_without_second_delivery(shop, reply, fulfill):
    r = await shop()
    old_description = r.api.item["description"]
    await configure(r, reply, fulfill)
    purchase(r)
    await drain(r)
    assert len(r.api.messages) == int(reply)
    assert r.api.orders["order"]["status"] == ("SENT" if fulfill else "PAID")
    assert not r.store.rows("SELECT * FROM reservations")
    assert not r.store.rows("SELECT * FROM inventory")
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind IN ('delivery','delivery_text','gift_send')")
    product = LogicalProducts(r.store, r.api).listing("lot")
    assert product["state"] == "VERIFIED"
    assert r.api.item["description"].removesuffix("\n" + product["code"]).strip() == old_description.strip()
    assert not r.api.remote.payments
    if fulfill:
        operation = r.store.rows("SELECT id FROM outbox WHERE kind='fulfill'")[0]["id"]
        await r.ui.click("result:show:" + operation)
        assert "Продавец отметил отправку" in r.session.last_panel.text
        assert "Статус не распознан" not in r.session.last_panel.text


@pytest.mark.parametrize("status", ["SENT", "CONFIRMED", "CONFIRMED_AUTOMATICALLY"])
async def test_already_marked_new_order_gets_one_reply_without_repeat_mark(shop, status):
    r = await shop()
    await configure(r)
    purchase(r, status=status)
    await drain(r)
    assert len(r.api.messages) == 1
    assert r.api.orders["order"]["status"] == status
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind='fulfill'")


async def test_ten_buyers_duplicates_restart_and_item_isolation(shop):
    r = await shop()
    r.api.items["second"] = {**deepcopy(r.api.item), "id": "second"}
    await configure(r, text="Первый товар")
    await configure(r, text="Второй товар", item="second")
    for i in range(10):
        value = purchase(r, str(i), item="lot" if i % 2 == 0 else "second")
        Orders(r.store).ingest(value)
    await asyncio.gather(EnrollmentWorker(r.store, r.api).once(), EnrollmentWorker(r.store, r.api).once())
    await drain(r)
    await drain(r)
    assert len(r.api.messages) == 10
    assert len({m["chat_id"] for m in r.api.messages}) == 10
    for message in r.api.messages:
        i = int(message["chat_id"].split("-")[-1])
        assert message["text"] == ("Первый товар" if i % 2 == 0 else "Второй товар")
    assert all(o["status"] == "SENT" for o in r.api.orders.values())


async def test_unknown_reply_blocks_repeat_and_does_not_lose_order(shop):
    r = await shop()
    await configure(r)
    purchase(r)
    send = r.api.account.send_message

    def lost(**kwargs):
        send(**kwargs)
        raise APIError("NETWORK_ERROR", uncertain=True)

    r.api.account.send_message = lost
    await drain(r)
    await drain(r)
    assert len(r.api.messages) == 1
    assert r.store.rows("SELECT status FROM outbox WHERE kind='chain_message'")[0]["status"] == "UNCERTAIN"
    assert r.api.orders["order"]["status"] == "PAID"
    assert r.store.rows("SELECT id FROM orders") == [{"id": "order"}]


async def test_historical_cancelled_stop_and_account_change_never_send(shop):
    r = await shop()
    purchase(r, "old")["created_at"] = "2020-01-01T00:00:00Z"
    Orders(r.store).ingest(r.api.orders["old"])
    await configure(r)
    purchase(r, "cancelled", status="ROLLED_BACK")
    purchase(r, "future")
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    Automation(r.store, r.api).stop_all(7)
    await drain(r)
    assert not r.api.messages
    await configure(r)
    purchase(r, "other-account")
    r.api.account.id = "unexpected"
    await drain(r)
    assert not r.api.messages


async def test_unknown_fulfillment_never_repeats_after_server_accepted(shop):
    r = await shop()
    await configure(r, reply=False)
    purchase(r)
    calls = []

    def lost(deal_id, new_status):
        calls.append(deal_id)
        r.api.update_deal(deal_id, new_status)
        raise APIError("NETWORK_ERROR", uncertain=True)

    r.api.account.update_deal = lost
    await drain(r)
    await drain(r)
    assert calls == ["order"]
    assert r.store.rows("SELECT status FROM outbox WHERE kind='fulfill'") == [{"status": "UNCERTAIN"}]


async def test_required_report_is_not_fabricated_and_reply_still_confirmed(shop):
    r = await shop()
    await configure(r)
    value = purchase(r)
    value["item"]["user"]["role"] = "OFFICIAL_SELLER"
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_fulfillment_requirement":
            return {"requirement": "fields"}
        if method == "get_fulfillment_entry":
            return {"state": "absent"}
        return await original(method, **kwargs)

    r.api.call = call
    await drain(r)
    assert len(r.api.messages) == 1
    assert r.api.orders["order"]["status"] == "PAID"
    assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "BLOCKED"}]
    await r.ui.click("order:order")
    assert "отчёт" in r.session.last_panel.text.lower()
    assert "aq:chain:order" in buttons(r.session.last_panel)
    await click(r, action="aq:chain:order")
    assert "Отметка продавца" in r.session.last_panel.text
    assert "Заполните его в сделке на Playerok" in r.session.last_panel.text
    assert not r.api.remote.payments


@pytest.mark.parametrize("previous_stock", [False, True])
async def test_confirmed_logical_relist_inherits_platform_policy_without_delivery(shop, previous_stock):
    r = await shop()
    if previous_stock:
        await setup_ui(r, "fixed_text", "Never send this old secret")
    await configure(r, text="Наследуемый ответ")
    r.api.item["status"] = "SOLD"
    r.api.items["relisted"] = {**deepcopy(r.api.item), "id": "relisted", "status": "APPROVED"}
    await LogicalProductWorker(r.store, r.api).once()
    assert Automation(r.store, r.api).get("relisted")[1].mode == "playerok"
    purchase(r, item="relisted")
    await drain(r)
    assert [m["text"] for m in r.api.messages] == ["Наследуемый ответ"]
    assert r.api.orders["order"]["status"] == "SENT"
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind IN ('delivery','delivery_text')")
    Automation(r.store, r.api).stop_all(7)
    assert not LogicalProducts(r.store, r.api).listing("relisted")["enabled"]
    assert not Automation(r.store, r.api).get("relisted")[1].enabled


async def test_same_name_without_marker_never_inherits(shop):
    r = await shop()
    await configure(r)
    r.api.items["unrelated"] = {**deepcopy(r.api.item), "id": "unrelated"}
    purchase(r, item="unrelated")
    await LogicalProductWorker(r.store, r.api).once()
    await drain(r)
    assert not r.api.messages
    assert not Automation(r.store, r.api).get("unrelated")[1].enabled


async def test_durable_input_restart_old_nonce_and_existing_item_card(shop):
    r = await shop()
    await r.ui.click("iv:delivery:lot")
    await click(r, action="lp:method:lot")
    await click(r, action="platform:item:lot")
    await click(r, suffix=":reply")
    await click(r, suffix=":text")
    r.ui.dp = production_dispatcher(r.store, r.api)
    await r.ui.message("Ответ после перезапуска")
    nonce = await click(r, suffix=":save")
    await finish_save(r)
    revision = Automation(r.store, r.api).get("lot")[0]
    await r.ui.click(nonce)
    assert Automation(r.store, r.api).get("lot")[0] == revision
    await r.ui.click("item:lot")
    assert "Товар выдаёт Playerok" in r.session.last_panel.text
    assert "После оплаты: включён" in r.session.last_panel.text
    assert not r.api.messages
    purchase(r)
    await EnrollmentWorker(r.store, r.api).once()
    from app.database.store import Store
    from app.services.products import Products
    from app.workers.outbox import Executor, OutboxWorker

    other = Store(r.store.path)
    try:
        ChainWorker(other, r.api).once()
        await OutboxWorker(other, Executor(other, r.api, Products(other, r.api))).once()
        ChainWorker(other, r.api).once()
    finally:
        other.close()
    await drain(r)
    assert [m["text"] for m in r.api.messages] == ["Ответ после перезапуска"]


async def test_positive_fulfillment_reconciliation_does_not_repeat_mutation(shop):
    r = await shop()
    await configure(r, reply=False)
    purchase(r)
    calls = []

    def lost(deal_id, new_status):
        calls.append(deal_id)
        r.api.update_deal(deal_id, new_status)
        raise APIError("NETWORK_ERROR", uncertain=True)

    r.api.account.update_deal = lost
    await drain(r)
    row = r.store.rows("SELECT * FROM outbox WHERE kind='fulfill'")[0]
    result = await Reconciliation(r.store, r.api).evidence(7, row["id"])
    assert result["remote_status"] == "SENT"
    assert calls == ["order"]


async def test_cancel_between_reply_and_mark_prevents_completion(shop):
    r = await shop()
    await configure(r)
    purchase(r)
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    await r.worker.once()
    r.api.orders["order"]["status"] = "ROLLED_BACK"
    await drain(r)
    assert len(r.api.messages) == 1
    assert r.api.orders["order"]["status"] == "ROLLED_BACK"
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind='fulfill' AND started_at IS NOT NULL")


@pytest.mark.parametrize("writes,automation", [(False, True), (True, False), (False, False)])
async def test_global_flags_never_activate_followups(shop, writes, automation):
    r = await shop()
    await configure(r)
    purchase(r)
    r.api.account.writes_enabled = writes
    r.api.account.automation_enabled = automation
    await drain(r)
    assert not r.api.messages
    assert not r.store.rows("SELECT * FROM delivery_chains")


async def test_unavailable_chat_has_concrete_reason_without_message(shop):
    r = await shop()
    await configure(r, fulfill=False)
    purchase(r)
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_chat":
            return {"id": kwargs["chat_id"], "is_texting_allowed": False}
        return await original(method, **kwargs)

    r.api.call = call
    await drain(r)
    assert not r.api.messages
    await r.ui.click("order:order")
    assert "Чат покупателя недоступен" in r.session.last_panel.text


async def test_preexisting_order_with_ahead_server_clock_is_not_enrolled(shop):
    r = await shop()
    value = purchase(r)
    value["created_at"] = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    Orders(r.store).ingest(value)
    await configure(r)
    await drain(r)
    assert not r.api.messages
    assert r.api.orders["order"]["status"] == "PAID"


async def test_owner_can_stop_pending_followup_without_waiting_for_delivery(shop):
    r = await shop()
    await configure(r)
    purchase(r)
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    await r.ui.click("platform:item:lot")
    await click(r, suffix=":reply")
    await click(r, suffix=":fulfill")
    await click(r, suffix=":save")
    assert not Automation(r.store, r.api).get("lot")[1].enabled
    await drain(r)
    assert not r.api.messages


async def test_two_outbox_workers_claim_followup_only_once(shop):
    r = await shop()
    await configure(r, fulfill=False)
    purchase(r)
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    from app.database.store import Store
    from app.workers.outbox import Executor, OutboxWorker

    other = Store(r.store.path)
    try:
        await asyncio.gather(r.worker.once(), OutboxWorker(other, Executor(other, r.api, None)).once())
        await drain(r)
    finally:
        other.close()
    assert len(r.api.messages) == 1


async def test_stop_during_message_rpc_preserves_result_and_blocks_next_step(shop):
    r = await shop()
    await configure(r)
    purchase(r)
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    entered, release = threading.Event(), threading.Event()
    original = r.api.account.send_message

    def delayed(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    r.api.account.send_message = delayed
    task = asyncio.create_task(r.worker.once())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        await r.ui.click("platform:item:lot")
        await click(r, suffix=":reply")
        await click(r, suffix=":fulfill")
        await click(r, suffix=":save")
    finally:
        release.set()
        await task
    await drain(r)
    assert len(r.api.messages) == 1
    assert r.store.rows("SELECT kind,status FROM outbox WHERE kind!='edit'") == [
        {"kind": "chain_message", "status": "SUCCEEDED"}
    ]
    assert r.api.orders["order"]["status"] == "PAID"


async def test_fulfillment_only_does_not_require_chat_or_read_goods(shop):
    r = await shop()
    await configure(r, reply=False)
    value = purchase(r)
    value.pop("chat")
    await drain(r)
    assert not r.api.messages
    assert r.api.orders["order"]["status"] == "SENT"
    assert not any(method == "get_chat" for method, _ in r.api.calls)


@pytest.mark.parametrize("status", ["PAID", "SENT"])
async def test_private_purchase_snapshot_uses_confirmed_logical_link_only(shop, status):
    r = await shop()
    await setup_ui(r, "fixed_text", "Must never be sent")
    await configure(r)
    r.api.items["private"] = {**deepcopy(r.api.item), "id": "private", "status": "SOLD"}
    r.api.item["status"] = "SOLD"
    purchase(r, status=status, item="private")
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_item" and kwargs["id"] == "private":
            raise APIError("HTTP_404")
        if method == "get_delivery_deal":
            return deepcopy(r.api.orders[kwargs["deal_id"]])
        return await original(method, **kwargs)

    r.api.call = call
    await LogicalProductWorker(r.store, r.api).once()
    assert Automation(r.store, r.api).get("private")[1].mode == "playerok"
    assert LogicalProducts(r.store, r.api).listing("private")["source_order_id"] == "order"
    await drain(r)
    assert len(r.api.messages) == 1
    assert r.api.orders["order"]["status"] == "SENT"
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind IN ('delivery','delivery_text','gift_send')")
    purchase(r, ident="different-order", item="private")
    await drain(r)
    assert len(r.api.messages) == 1
    assert r.api.orders["different-order"]["status"] == "PAID"
