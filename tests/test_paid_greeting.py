from datetime import UTC, datetime, timedelta

import pytest

from app.services.orders import Orders
from app.services.paid_greeting import render
from app.workers.notifications import Notifications
from tests.test_unified_delivery_controls import shop as shop_fixture

shop = shop_fixture


@pytest.mark.parametrize(
    "fields,expected",
    [
        ([], ""),
        ([{"id": "tg", "label": "Telegram", "value": "@buyer"}], "Telegram: @buyer"),
        (
            [
                {"id": "tg", "label": "Telegram", "value": "@buyer"},
                {"id": "other", "label": "Регион", "value": "Europe"},
            ],
            "Регион: указано",
        ),
        ([{"id": "tg", "label": "Telegram", "required": True}], "для выполнения заказа укажи"),
    ],
)
def test_universal_greeting_uses_only_safe_fields(fields, expected):
    text = render({"item": {"name": "🎁 Сердечко [9T4]"}, "obtaining_fields": fields})
    assert "спасибо за покупку" in text and "[9T4]" not in text
    assert expected in text
    assert ("📝 данные заказа:" in text) == bool(fields and fields[0].get("value"))
    assert "Europe" not in text


async def test_paid_greeting_is_durable_deduplicated_and_payment_system_notice_is_silent(shop):
    r = await shop()
    r.store.set_setting("orders:greeting_since", (datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    local = Orders(r.store)
    for ident in ("one", "two"):
        order = r.api.order(ident)
        local.ingest(order)
        local.ingest(order)
    local.ingest_message("chat-one", {"id": "paid-system", "text": "{{ITEM_PAID}}"})
    for _ in range(4):
        await r.worker.once()
    assert len(r.api.messages) == 2
    assert all("заказ принят в обработку" in message["text"] for message in r.api.messages)
    await Notifications(r.store, r.bot).once()
    assert not any("Оплата заказа подтверждена" in (getattr(c, "text", "") or "") for c in r.session.calls)


async def test_refresh_catalog_displays_fresh_new_listing_on_same_callback(shop):
    from copy import deepcopy

    from app.services.item_filters import catalog_page

    r = await shop()
    key = "ui:catalog:" + r.api.account.id
    r.store.set_setting(key, {**await catalog_page(r.api, r.api.account.id, {}), "background": True})
    r.api.items["new-lot"] = {**deepcopy(r.api.item), "id": "new-lot", "name": "Новый лот с сайта"}
    await r.ui.click("il:refreshsearch")
    from tests.test_ux_redesign import buttons

    assert "item:new-lot" in buttons(r.session.last_panel)


@pytest.mark.parametrize("mode,status", [("unique_stock", "SOLD"), ("telegram_gifts", "PENDING_MODERATION")])
async def test_verified_inactive_listing_can_arm_future_delivery_without_dispatch(shop, mode, status):
    from app.services.logical_products import LogicalProducts
    from app.services.product_readiness import ProductReadiness
    from tests.test_universal_delivery_e2e import setup_ui

    r = await shop(gift=mode == "telegram_gifts")
    await setup_ui(r, mode, "gift: heart" if mode == "telegram_gifts" else None)
    service = LogicalProducts(r.store, r.api)
    old = service.listing("lot")
    service.stop(7, old["id"])
    r.api.item["status"] = status
    await service.arm(7, old["id"], old["revision"], item_id="lot")
    assert service.listing("lot")["enabled"]
    view = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
    assert view["state"] == "WAITING_PUBLICATION"
    assert not r.api.messages and not r.api.remote.payments


async def test_greeting_lost_response_never_replays_after_restart(shop):
    from app.playerok.adapter import APIError
    from tests.test_logical_faults import reopen

    r = await shop()
    r.store.set_setting("orders:greeting_since", (datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    order = r.api.order("lost")
    Orders(r.store).ingest(order)
    send = r.api.account.send_message

    def lost(**kwargs):
        send(**kwargs)
        raise APIError("NETWORK_ERROR", uncertain=True)

    r.api.account.send_message = lost
    await r.worker.once()
    reopen(r)
    Orders(r.store).ingest(order)
    await r.worker.once()
    assert len(r.api.messages) == 1
    assert r.store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]


@pytest.mark.parametrize("mode", ["unique_stock", "telegram_gifts"])
async def test_greeting_failure_never_blocks_delivery_or_enqueues_legacy_greeting(shop, mode):
    from app.gifts.worker import GiftWorker
    from app.playerok.adapter import APIError
    from app.services.logical_products import LogicalProducts
    from app.workers.delivery_chains import ChainWorker
    from app.workers.enrollment import EnrollmentWorker
    from tests.test_universal_delivery_e2e import setup_ui

    r = await shop(gift=mode == "telegram_gifts")
    await setup_ui(r, mode, "gift: heart" if mode == "telegram_gifts" else None)
    if mode == "unique_stock":
        logical = LogicalProducts(r.store, r.api)
        logical.update_after(7, "lot", logical.listing("lot"), "Старое приветствие", field="greeting")
    r.store.set_setting("orders:greeting_since", (datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    order = r.api.order("paid")
    Orders(r.store).ingest(order)
    send = r.api.account.send_message

    def lost(**kwargs):
        if "спасибо за покупку! ♡" in kwargs["text"]:
            raise APIError("NETWORK_ERROR", uncertain=True)
        return send(**kwargs)

    r.api.account.send_message = lost
    for _ in range(10):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
        await r.worker.once()
    assert not any("Старое приветствие" in m["text"] for m in r.api.messages)
    assert r.store.rows("SELECT status FROM outbox WHERE operation_key LIKE 'paid-greeting:%'") == [
        {"status": "UNCERTAIN"}
    ]
    if mode == "telegram_gifts":
        assert len(r.api.remote.payments) == 1
    else:
        assert r.store.rows("SELECT status FROM outbox WHERE kind='delivery'") == [{"status": "SUCCEEDED"}]
