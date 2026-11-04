"""Temporary pause preserves approved post-delivery completion, unlike STOP."""

import pytest

from app.gifts.worker import GiftWorker
from app.services.orders import Orders
from tests.test_stability_deal_completeness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui


@pytest.fixture
async def shop(tmp_path):
    async for create in shop_fixture.__wrapped__(tmp_path):
        yield create


@pytest.mark.parametrize("emergency", [False, True])
async def test_completion_after_delivered_gift_distinguishes_pause_and_stop(shop, emergency):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart", completion=True)
    Orders(r.store).ingest(r.api.order("completion-pause"))
    worker = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    await worker.once()
    await r.worker.once()
    assert len(r.api.remote.payments) == 1
    assert r.store.rows("SELECT state,completion_state FROM gift_jobs") == [
        {"state": "SUCCEEDED", "completion_state": "WAITING"}
    ]
    worker.service.pause(7, True)
    if emergency:
        worker.service.stop(7)
    await worker.once()
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind='fulfill'")
    assert r.store.rows("SELECT completion_state FROM gift_jobs") == [
        {"completion_state": "MANUAL" if emergency else "WAITING"}
    ]
    worker.service.pause(7, False)
    for _ in range(4):
        await worker.once()
        await r.worker.once()
    assert r.api.orders["completion-pause"]["status"] == ("PAID" if emergency else "SENT")
    assert len(r.api.remote.payments) == 1


@pytest.mark.parametrize("failure", ["connection", "sync"])
async def test_temporary_runtime_loss_does_not_revoke_already_approved_completion(shop, failure):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart", completion=True)
    Orders(r.store).ingest(r.api.order("completion-recovery"))
    worker = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    await worker.once()
    await r.worker.once()
    if failure == "connection":
        r.api.account.writes_enabled = False
    else:
        r.store.set_setting("sync:health", {"status": "ERROR", "errors": {"orders": "NETWORK_ERROR"}})
    await worker.once()
    assert r.store.rows("SELECT completion_state FROM gift_jobs") == [{"completion_state": "WAITING"}]
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind='fulfill'")
    r.api.account.writes_enabled = True
    r.store.set_setting("sync:health", {"status": "OK"})
    for _ in range(5):
        await worker.once()
        await r.worker.once()
    assert r.api.orders["completion-recovery"]["status"] == "SENT"
    assert len(r.api.remote.payments) == 1
