"""Actual paused payment boundary and owner STOP, no simulated business service."""

import asyncio

from telethon import functions

from app.gifts.worker import GiftWorker
from app.services.orders import Orders
from app.services.product_readiness import ProductReadiness
from app.telegram.home_panel import HomePanel
from app.telegram.product_presentation import summary
from tests.test_stage_a_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import click, setup_ui

shop = shop_fixture


async def test_inflight_payment_waits_without_claiming_unknown_or_owner_intervention(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    Orders(r.store).ingest(r.api.order("normal-inflight"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    entered, release = asyncio.Event(), asyncio.Event()
    original = r.api.remote

    class Boundary:
        def __getattr__(self, name):
            return getattr(original, name)

        async def __call__(self, request):
            if isinstance(request, functions.payments.SendStarsFormRequest):
                entered.set()
                await release.wait()
            return await original(request)

    r.api.gift_sender.client = Boundary()
    task = asyncio.create_task(r.worker.once())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        result = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
        assert result["state"] != "STOPPED"
        assert not result["manual"]
        text = summary(result, compact=True)
        assert "требуется решение" not in text
        assert "Требует внимания:" not in text
        assert "ожида" in text.lower()
    finally:
        release.set()
        await task
    assert len(r.api.remote.payments) == 1
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]


async def test_stopped_item_keeps_its_paid_pending_order_visible_without_telegram_delivery(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    Orders(r.store).ingest(r.api.order("paid-before-stop"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":stop")
    issues = HomePanel(r.store, r.api).paid_issues()
    assert len(issues) == 1 and issues[0]["id"] == "paid-before-stop"
    assert "выключена" in HomePanel.paid_reason(issues[0])
    await r.worker.once()
    assert not r.api.remote.payments


async def test_local_stop_is_obeyed_by_gift_worker_not_only_readiness(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    r.store.set_setting("delivery:lot:enabled", False)
    Orders(r.store).ingest(r.api.order("local-stop-paid"))
    result = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
    assert not result["ready"]
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert not r.api.remote.payments
    assert HomePanel(r.store, r.api).paid_issues()[0]["id"] == "local-stop-paid"
