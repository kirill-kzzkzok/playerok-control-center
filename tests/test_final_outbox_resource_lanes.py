"""One shared durable Outbox must not serialize unrelated currencies/transports."""

import asyncio
from copy import deepcopy

import pytest
from telethon import functions

from app.__main__ import outbox_loop
from app.database.store import Store
from app.gifts.mtproto import PersonalGiftSender
from app.gifts.worker import GiftWorker
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_gift_bundles import BundleTransport
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import click, setup_ui

shop = shop_fixture


class DelayedPayment(BundleTransport):
    def __init__(self):
        super().__init__()
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def __call__(self, request):
        if isinstance(request, functions.payments.SendStarsFormRequest):
            self.entered.set()
            await self.release.wait()
        return await super().__call__(request)


async def configure_other_item(r, ident, handler):
    r.api.items[ident] = {
        **deepcopy(r.api.item),
        "id": ident,
        "name": "Independent product " + ident,
    }
    await r.ui.click("lp:method:" + ident)
    await click(r, suffix=":mode:" + handler)
    if handler == "fixed_text":
        await r.ui.message("isolated-fixed-text")
    else:
        await click(r, suffix=":stockinput")
        await r.ui.message("isolated-stock-secret")
        await click(r, suffix=":stock_commit")
    await click(r, suffix=":preview")
    await click(r, suffix=":commit")
    await r.worker.once()
    await r.ui.click("lp:item:" + ident)
    await click(r, suffix=":arm")
    await click(r, suffix=":enable:this")


async def test_blocked_gift_rpc_does_not_delay_independent_stock_and_text_delivery(shop):
    r = await shop(gift=True)
    remote = DelayedPayment()
    r.api.remote = remote
    r.api.gift_sender = PersonalGiftSender(r.api.gift_settings, client=remote)

    def update(id, **changes):
        r.api.mutations.append(deepcopy(changes))
        r.api.items[id].update(changes)
        return deepcopy(r.api.items[id])

    r.api.account.update_item = update
    await setup_ui(r, "telegram_gifts", "gift: heart")
    await configure_other_item(r, "stock", "unique_stock")
    await configure_other_item(r, "text", "fixed_text")
    Orders(r.store).ingest(r.api.order("gift-order"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    stop = asyncio.Event()
    task = asyncio.create_task(outbox_loop(r.worker, stop))
    try:
        await asyncio.wait_for(remote.entered.wait(), 3)
        assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SPENDING"}]
        for ident in ("stock", "text"):
            Orders(r.store).ingest(r.api.order(ident + "-order", ident))
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        async with asyncio.timeout(4):
            while not {"isolated-stock-secret", "isolated-fixed-text"} <= {m["text"] for m in r.api.messages}:
                await asyncio.sleep(0.02)
        assert not remote.release.is_set()
        assert len(r.store.rows("SELECT * FROM reservations")) == 1
    finally:
        stop.set()
        remote.release.set()
        await asyncio.wait_for(task, 5)
    assert len(remote.payments) == 1
    assert len([m for m in r.api.messages if m["text"] == "isolated-stock-secret"]) == 1
    assert len([m for m in r.api.messages if m["text"] == "isolated-fixed-text"]) == 1


@pytest.mark.parametrize("outcome", ["cancel", "lease", "stop"])
async def test_stars_lane_is_serial_and_unknown_restart_does_not_repeat(shop, outcome):
    r = await shop(gift=True)
    remote = DelayedPayment()
    r.api.remote = remote
    r.api.gift_sender = PersonalGiftSender(r.api.gift_settings, client=remote)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    for ident in ("first", "second"):
        Orders(r.store).ingest(r.api.order(ident))
    gifts = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    await gifts.once()
    stop = asyncio.Event()
    task = asyncio.create_task(outbox_loop(r.worker, stop))
    await asyncio.wait_for(remote.entered.wait(), 3)
    assert len(r.store.rows("SELECT * FROM outbox WHERE kind='gift_send' AND status='PROCESSING'")) == 1
    # Reconsidering the second order cannot start another Stars payment.
    await gifts.once()
    assert sum(row["state"] == "SPENDING" for row in r.store.rows("SELECT state FROM gift_jobs")) == 1
    if outcome == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        if outcome == "lease":
            r.store.execute("UPDATE outbox SET lease_until=0 WHERE kind='gift_send' AND status='PROCESSING'")
            r.store.recover()
        else:
            gifts.service.stop(7)
        stop.set()
        remote.release.set()
        await asyncio.wait_for(task, 5)
    pending = r.store.rows("SELECT status FROM outbox WHERE kind='gift_send' ORDER BY created_at")
    expected = "SUCCEEDED" if outcome == "stop" else "UNCERTAIN"
    assert pending[0]["status"] == expected
    payment_count = len(remote.payments)
    # Reopen a separate connection, as after restart; do not reconstruct business state.
    reopened = Store(r.store.path)
    try:
        assert (
            reopened.rows("SELECT status FROM outbox WHERE kind='gift_send' ORDER BY created_at")[0]["status"]
            == expected
        )
    finally:
        reopened.close()
    await gifts.once()
    await r.worker.once(kinds=("gift_send",))
    assert len(remote.payments) == payment_count
