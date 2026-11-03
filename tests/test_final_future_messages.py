"""Future-only message edits through Dispatcher and real delivery workers."""

import json

import pytest

from app.gifts.worker import GiftWorker
from app.playerok.adapter import APIError
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_telegram_transport import production_dispatcher
from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


async def change_message(r, value):
    await r.ui.click("lp:messages:lot")
    await r.ui.message(value)


@pytest.mark.parametrize("phase", ["paid", "reserved", "queued"])
async def test_future_after_preserves_old_order_and_delivers_new_text_only_to_new_order(shop, phase):
    r = await shop()
    await setup_ui(r, "unique_stock")
    logical = LogicalProducts(r.store, r.api)
    original = logical.listing("lot")
    Orders(r.store).ingest(r.api.order("old"))
    if phase in {"reserved", "queued"}:
        await EnrollmentWorker(r.store, r.api).once()
    if phase == "queued":
        ChainWorker(r.store, r.api).once()
    reservations = r.store.rows("SELECT * FROM reservations")
    operations = r.store.rows("SELECT id,payload FROM outbox")
    await change_message(r, "Новое спасибо только для новых покупок")
    changed = logical.listing("lot")
    assert json.loads(changed["config"])["after"] == "Новое спасибо только для новых покупок"
    assert changed["enabled"] == original["enabled"] == 1
    assert changed["armed_at"] == original["armed_at"]
    assert changed["control_revision"] == original["control_revision"]
    assert r.store.rows("SELECT * FROM reservations") == reservations
    assert r.store.rows("SELECT id,payload FROM outbox") == operations
    Orders(r.store).ingest(r.api.order("new"))
    await EnrollmentWorker(r.store, r.api).once()
    for _ in range(12):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "SUCCEEDED"}] * 2
    old = [m["text"] for m in r.api.messages if m["chat_id"] == "chat-old"]
    new = [m["text"] for m in r.api.messages if m["chat_id"] == "chat-new"]
    assert old[-1] == "Спасибо за покупку!"
    assert new[-1] == "Новое спасибо только для новых покупок"
    assert len(old) == len(new) == 2
    assert not r.api.remote.payments


async def test_stop_during_message_input_wins_and_keeps_order_snapshot(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    await r.ui.click("lp:messages:lot")
    before = LogicalProducts(r.store, r.api).listing("lot")
    LogicalProducts(r.store, r.api).stop(7, before["id"])
    await r.ui.message("Не должно восстановить выдачу")
    after = LogicalProducts(r.store, r.api).listing("lot")
    assert after["config"] == before["config"]
    assert not after["enabled"]
    assert not r.api.messages


@pytest.mark.parametrize("known_job", [False, True])
async def test_gift_message_update_preserves_price_authority_and_old_receipt_flow(shop, known_job):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    service = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    Orders(r.store).ingest(r.api.order("old"))
    if known_job:
        await service.once()
    authority = r.store.rows("SELECT revision,enabled,armed_at,bundle FROM gift_listings")
    await change_message(r, "Новое сообщение после подарка")
    assert r.store.rows("SELECT revision,enabled,armed_at,bundle FROM gift_listings") == authority
    assert not r.api.remote.payments and not r.api.messages
    r.ui.dp = production_dispatcher(r.store, r.api)
    Orders(r.store).ingest(r.api.order("new"))
    for _ in range(10):
        await service.once()
        await r.worker.once()
    assert [m["text"] for m in r.api.messages if m["chat_id"] == "chat-old"] == ["Спасибо за покупку!"]
    assert [m["text"] for m in r.api.messages if m["chat_id"] == "chat-new"] == [
        "Новое сообщение после подарка"
    ]
    assert len(r.api.remote.payments) == 2
    assert all(
        row["state"] == "SUCCEEDED" and row["receipt"] for row in r.store.rows("SELECT * FROM gift_jobs")
    )
    for _ in range(3):
        await service.once()
        await r.worker.once()
    assert len(r.api.remote.payments) == 2


async def test_message_update_does_not_restart_unknown_inventory_send(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    Orders(r.store).ingest(r.api.order("unknown"))
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    send = r.api.account.send_message

    def lost(**kwargs):
        send(**kwargs)
        raise APIError("NETWORK_ERROR", uncertain=True)

    r.api.account.send_message = lost
    await r.worker.once()
    before = r.store.rows("SELECT * FROM outbox")
    reserve = r.store.rows("SELECT * FROM inventory WHERE status='QUARANTINED'")
    assert reserve
    await change_message(r, "Текст для следующих покупок")
    assert r.store.rows("SELECT * FROM outbox") == before
    assert r.store.rows("SELECT * FROM inventory WHERE status='QUARANTINED'") == reserve
    for _ in range(3):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert len(r.api.messages) == 1
    assert r.store.rows("SELECT status FROM outbox WHERE kind='delivery'") == [{"status": "UNCERTAIN"}]


async def test_concurrent_message_edit_cannot_overwrite_newer_input_or_sneak_in_secret_changes(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "secret-inventory-text")
    logical = LogicalProducts(r.store, r.api)
    previous = logical.listing("lot")
    logical.update_after(7, "lot", previous, "Первое изменение")
    with pytest.raises(ValueError, match="изменились"):
        logical.update_after(7, "lot", previous, "Устаревшее изменение")
    current = logical.listing("lot")
    assert json.loads(current["config"])["text"] == "secret-inventory-text"
    assert json.loads(current["config"])["after"] == "Первое изменение"
    Orders(r.store).ingest(r.api.order("paid"))
    await EnrollmentWorker(r.store, r.api).once()
    altered = json.loads(current["config"])
    altered["mark_sent"] = not altered.get("mark_sent", False)
    r.store.execute("UPDATE logical_products SET config=? WHERE id=?", (json.dumps(altered), current["id"]))
    with pytest.raises(ValueError, match="Конфигурация"):
        logical.require_delivery("lot", order_id="paid")


async def test_after_update_cannot_restart_unknown_star_payment_after_restart(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    gifts = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    Orders(r.store).ingest(r.api.order("unknown-payment"))
    r.api.remote.fail_at = 0
    await gifts.once()
    await r.worker.once()
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "UNCERTAIN"}]
    state = r.store.rows("SELECT * FROM gift_jobs")
    budget = r.store.rows("SELECT revision,enabled,armed_at,bundle FROM gift_listings")
    await change_message(r, "Сообщение не разрешает повторный платёж")
    r.ui.dp = production_dispatcher(r.store, r.api)
    for _ in range(4):
        await gifts.once()
        await r.worker.once()
    assert r.store.rows("SELECT * FROM gift_jobs") == state
    assert r.store.rows("SELECT revision,enabled,armed_at,bundle FROM gift_listings") == budget
    assert len(r.api.remote.payments) == 1
    assert not r.api.messages
