"""Bounded delivery fault journeys; real services, only API transports simulated."""

import asyncio
import copy

import pytest

from app.database.store import Store
from app.gifts.mtproto import GiftFailure
from app.gifts.service import GiftService
from app.gifts.worker import GiftWorker
from app.playerok.adapter import APIError
from app.services.inventory import Inventory
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.services.products import Products
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.logical_products import LogicalProductWorker
from app.workers.outbox import Executor, OutboxWorker
from app.workers.sync import Synchronizer
from tests.test_telegram_transport import production_dispatcher
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_universal_delivery_e2e import simulation as simulation_fixture


@pytest.fixture
async def simulation(tmp_path):
    async for create in simulation_fixture.__wrapped__(tmp_path):
        yield create


def reopen(r):
    path = r.store.path
    r.store.close()
    r.store = Store(path)
    r.ui.dp = production_dispatcher(r.store, r.api)
    r.worker = OutboxWorker(r.store, Executor(r.store, r.api, Products(r.store, r.api)))


async def cycle(r, count=12):
    for _ in range(count):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
        await r.worker.once()


@pytest.mark.parametrize(
    "handler,value",
    [
        ("fixed_text", "unique fixed payload"),
        ("unique_stock", None),
        ("telegram_gifts", "gift: heart, heart"),
    ],
)
async def test_paid_replays_poll_requests_and_restart_do_not_repeat_delivery_or_thanks(
    simulation, handler, value
):
    r = await simulation(gift=handler == "telegram_gifts")
    await setup_ui(r, handler, value)
    order = r.api.order("replayed")
    for _ in range(2):
        Orders(r.store).ingest(copy.deepcopy(order))
    sync = Synchronizer(r.store, r.api, Orders(r.store))
    sync.request("deal", "replayed")
    sync.request("deal", "replayed")
    await sync.process_requests()
    await sync.once()
    await cycle(r)
    messages, payments = copy.deepcopy(r.api.messages), len(r.api.remote.payments)
    assert [m["text"] for m in messages].count("Спасибо за покупку!") == 1
    if handler == "telegram_gifts":
        assert payments == 2
        assert [s["state"] for s in r.store.rows("SELECT state FROM gift_steps ORDER BY position")] == [
            "SUCCEEDED",
            "SUCCEEDED",
        ]
    else:
        assert len(messages) == 2
    reopen(r)
    for _ in range(2):
        Orders(r.store).ingest(copy.deepcopy(order))
        await Synchronizer(r.store, r.api, Orders(r.store)).once()
        await cycle(r, 3)
    assert r.api.messages == messages and len(r.api.remote.payments) == payments
    assert len(r.store.rows("SELECT * FROM logical_order_scopes")) == 1
    if handler == "unique_stock":
        assert Inventory(r.store, r.api).counts("lot")["DELIVERED"] == 1
        assert len(r.store.rows("SELECT * FROM reservations")) == 1


@pytest.mark.parametrize("lost_position", [0, 1])
async def test_logical_gift_lost_payment_ack_blocks_later_positions_across_restart(simulation, lost_position):
    r = await simulation(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart, heart, bear")
    r.api.remote.fail_at = lost_position
    order = r.api.order("unknown-payment")
    await Synchronizer(r.store, r.api, Orders(r.store)).once()
    await cycle(r)
    steps = r.store.rows("SELECT * FROM gift_steps ORDER BY position")
    assert [s["state"] for s in steps[:lost_position]] == ["SUCCEEDED"] * lost_position
    assert all(s["message_id"] and s["receipt"] for s in steps[:lost_position])
    assert steps[lost_position]["state"] == "UNCERTAIN"
    assert all(s["state"] == "PENDING" and s["spent_at"] is None for s in steps[lost_position + 1 :])
    assert len(r.api.remote.payments) == lost_position + 1
    assert len(r.api.remote.messages) == lost_position + 1  # Remote performed it, but ACK was lost.
    assert not r.api.messages
    reopen(r)
    r.api.remote.fail_at = None
    r.api.remote.pay_error = None
    Orders(r.store).ingest(copy.deepcopy(order))
    await Synchronizer(r.store, r.api, Orders(r.store)).once()
    await cycle(r)
    with pytest.raises(GiftFailure, match="DELIVERY_CORRELATION_UNKNOWN"):
        await GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender).reconcile(
            7, "unknown-payment"
        )
    assert len(r.api.remote.payments) == lost_position + 1
    assert not r.api.messages
    assert not r.store.rows("SELECT * FROM outbox WHERE kind IN ('gift_refund','fulfill','gift_notice')")
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "UNCERTAIN"}]


async def test_stock_thanks_timeout_keeps_delivery_confirmed_and_never_repeats_message(simulation):
    r = await simulation()
    await setup_ui(r, "unique_stock")
    original = r.api.account.send_message
    attempts = []

    def lose_thanks_ack(chat_id, text):
        attempts.append((chat_id, text))
        message = original(chat_id, text)
        if text == "Спасибо за покупку!":
            raise APIError("NETWORK_ERROR", uncertain=True)
        return message

    r.api.account.send_message = lose_thanks_ack
    r.api.order("thanks-unknown")
    await Synchronizer(r.store, r.api, Orders(r.store)).once()
    await cycle(r)
    assert Inventory(r.store, r.api).counts("lot")["DELIVERED"] == 1
    operations = r.store.rows(
        "SELECT kind,status FROM outbox WHERE target='thanks-unknown' ORDER BY created_at"
    )
    assert operations == [
        {"kind": "delivery", "status": "SUCCEEDED"},
        {"kind": "chain_message", "status": "UNCERTAIN"},
    ]
    assert [m["text"] for m in r.api.messages].count("Спасибо за покупку!") == 1
    assert len(attempts) == 2
    reopen(r)
    await Synchronizer(r.store, r.api, Orders(r.store)).once()
    await cycle(r)
    assert len(attempts) == 2
    assert Inventory(r.store, r.api).counts("lot")["DELIVERED"] == 1
    assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "BLOCKED"}]
    assert not r.store.rows("SELECT * FROM outbox WHERE kind='fulfill'")


@pytest.mark.parametrize("relisted_id", ["lot", "replacement"])
async def test_same_and_new_item_id_relisting_keep_one_product_and_independent_orders(
    simulation, relisted_id
):
    r = await simulation()
    await setup_ui(r, "fixed_text", "same logical payload")
    product_id = LogicalProducts(r.store, r.api).listing("lot")["id"]
    r.api.order("first")
    await Synchronizer(r.store, r.api, Orders(r.store)).once()
    await cycle(r)
    if relisted_id != "lot":
        r.api.item["status"] = "SOLD"
        r.api.items[relisted_id] = {**copy.deepcopy(r.api.item), "id": relisted_id, "status": "APPROVED"}
    await LogicalProductWorker(r.store, r.api).once()
    assert LogicalProducts(r.store, r.api).listing(relisted_id)["id"] == product_id
    r.api.order("second", relisted_id)
    await Synchronizer(r.store, r.api, Orders(r.store)).once()
    await cycle(r)
    assert len(r.store.rows("SELECT * FROM logical_products")) == 1
    assert len(r.store.rows("SELECT * FROM logical_listings")) == (1 if relisted_id == "lot" else 2)
    for ident in ("first", "second"):
        assert [m["text"] for m in r.api.messages if m["chat_id"] == "chat-" + ident] == [
            "same logical payload",
            "Спасибо за покупку!",
        ]
    assert len(r.store.rows("SELECT * FROM logical_order_scopes")) == 2


async def test_four_logical_products_share_workers_without_mixing_orders_stock_or_gifts(simulation):
    r = await simulation(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    service = LogicalProducts(r.store, r.api)
    product_for_item = {"lot": service.listing("lot")["id"]}

    def update_item(id, **changes):
        r.api.mutations.append(copy.deepcopy(changes))
        r.api.items[id].update(changes)
        return copy.deepcopy(r.api.items[id])

    r.api.account.update_item = update_item
    specifications = [
        ("gift-two", "telegram_gifts", {"code": "bear, heart", "after": "Thanks gift two"}),
        ("stock", "unique_stock", {"after": "Thanks stock"}),
        ("text", "fixed_text", {"text": "Fixed text exclusive", "after": "Thanks text"}),
    ]
    for ident, handler, config in specifications:
        r.api.items[ident] = {
            **copy.deepcopy(r.api.item),
            "id": ident,
            "name": "Distinct product " + ident,
            "description": "Product description " + ident,
        }
        if handler != "telegram_gifts":
            r.api.items[ident].update(category={"id": "digital"}, obtaining_type={"id": "text-transfer"})
        prepared = await service.prepare(7, ident, handler, config)
        result = service.commit(7, prepared)
        assert await r.worker.once()
        product = service.product(result["product_id"])
        assert product["state"] == "VERIFIED" and not product["enabled"]
        if handler == "unique_stock":
            assert Orders(r.store).add_stock(ident, ["Stock exclusive one", "Stock exclusive two"]) == 2
        await service.arm(7, product["id"], product["revision"], inherit=True)
        product_for_item[ident] = product["id"]
    assert len(set(product_for_item.values())) == 4
    assert not r.api.remote.payments and not r.api.messages
    # Events arrive concurrently; the existing shared Outbox retains payment serialization.
    await asyncio.gather(
        *(asyncio.to_thread(r.api.order, "paid-" + ident, ident) for ident in product_for_item)
    )
    await Synchronizer(r.store, r.api, Orders(r.store)).once()
    await cycle(r, 25)
    scopes = r.store.rows("SELECT * FROM logical_order_scopes")
    assert len(scopes) == 4
    assert all(row["product_id"] == product_for_item[row["item_id"]] for row in scopes)
    steps = r.store.rows(
        "SELECT order_id,position,code,state,message_id FROM gift_steps ORDER BY order_id,position"
    )
    assert [(s["order_id"], s["code"]) for s in steps] == [
        ("paid-gift-two", "bear"),
        ("paid-gift-two", "heart"),
        ("paid-lot", "heart"),
    ]
    assert all(s["state"] == "SUCCEEDED" for s in steps)
    assert len({s["message_id"] for s in steps}) == 3
    assert len(r.api.remote.payments) == 3
    for ident in ("stock", "text"):
        texts = [m["text"] for m in r.api.messages if m["chat_id"] == "chat-paid-" + ident]
        assert texts == (
            ["Stock exclusive one", "Thanks stock"]
            if ident == "stock"
            else ["Fixed text exclusive", "Thanks text"]
        )
    assert [m["text"] for m in r.api.messages if m["chat_id"] == "chat-paid-lot"] == ["Спасибо за покупку!"]
    assert [m["text"] for m in r.api.messages if m["chat_id"] == "chat-paid-gift-two"] == ["Thanks gift two"]
    assert Inventory(r.store, r.api).counts("stock")["DELIVERED"] == 1
    assert Inventory(r.store, r.api).counts("stock")["AVAILABLE"] == 1
    assert len(r.store.rows("SELECT * FROM reservations")) == 1
