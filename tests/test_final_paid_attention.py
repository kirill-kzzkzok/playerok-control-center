"""Attention is driven by paid obligations, not notification delivery."""

from aiogram.methods import SendMessage

from app.playerok.adapter import APIError
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.notifications import Notifications
from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_missing_gift_recipient_is_requested_automatically_without_owner_intervention(shop):
    from app.gifts.worker import GiftWorker

    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    r.store.set_setting("notifications:initialized", True)
    order = r.api.order("missing-recipient")
    order["obtaining_fields"] = []
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_chat":
            return {
                "id": order["chat"]["id"],
                "type": "PM",
                "is_texting_allowed": True,
                "users": [order["user"], {"id": r.api.account.id}],
            }
        return await original(method, **kwargs)

    r.api.call = call
    Orders(r.store).ingest(order)
    await EnrollmentWorker(r.store, r.api).once()
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    await Notifications(r.store, r.bot).once()
    await r.ui.click("attention")
    assert "order:missing-recipient" not in buttons(r.session.last_panel)
    await r.worker.once()
    assert any("@username" in message["text"] for message in r.api.messages)
    await r.ui.click("order:missing-recipient")
    assert "Статус: Оплачен" in r.session.last_panel.text
    assert not r.api.remote.payments
    assert r.store.rows("SELECT 1 FROM notifications WHERE status='SUCCEEDED'")
    # Repeated observations and fresh worker instances cannot guess a recipient.
    Orders(r.store).ingest(order)
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert not r.api.remote.payments


async def test_blocked_paid_order_is_visible_even_if_notification_is_missing(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    r.api.item["attributes"] = {"delivery-critical": "changed"}
    Orders(r.store).ingest(r.api.order("blocked-paid"))
    await EnrollmentWorker(r.store, r.api).once()
    assert r.store.rows("SELECT status FROM auto_enrollment") == [{"status": "BLOCKED"}]
    assert not r.store.rows("SELECT * FROM notifications")
    await r.ui.click("home")
    assert "orders" in buttons(r.session.last_panel)
    await r.ui.click("orders")
    assert "attention" in buttons(r.session.last_panel)
    await r.ui.click("attention")
    assert "order:blocked-paid" in buttons(r.session.last_panel)
    assert "1" in r.session.last_panel.text
    await r.ui.click("order:blocked-paid")
    assert "Статус: Оплачен" in r.session.last_panel.text
    assert "характеристики или поля получения" in r.session.last_panel.text
    assert not r.api.messages and not r.api.remote.payments


async def test_unknown_stock_send_and_blocked_chain_remain_visible_when_telegram_notice_fails(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    r.store.set_setting("notifications:initialized", True)
    Orders(r.store).ingest(r.api.order("unknown-delivery"))
    await EnrollmentWorker(r.store, r.api).once()
    chains = ChainWorker(r.store, r.api)
    chains.once()

    def lost_reply(chat_id, text):
        r.api.messages.append({"chat_id": chat_id, "text": text})
        raise APIError("NETWORK_ERROR", uncertain=True)

    r.api.account.send_message = lost_reply
    await r.worker.once()
    chains.once()
    assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "BLOCKED"}]
    assert r.store.rows("SELECT status FROM inventory WHERE status='QUARANTINED'") == [
        {"status": "QUARANTINED"}
    ]
    send = r.session.make_request

    async def lost_notice(bot, method, timeout=None):
        if isinstance(method, SendMessage):
            raise TimeoutError()
        return await send(bot, method, timeout)

    r.session.make_request = lost_notice
    await Notifications(r.store, r.bot).once()
    r.session.make_request = send
    await r.ui.click("home")
    assert "orders" in buttons(r.session.last_panel)
    await r.ui.click("orders")
    assert "attention" in buttons(r.session.last_panel)
    await r.ui.click("attention")
    assert "order:unknown-delivery" in buttons(r.session.last_panel)
    assert "неизвестен" in r.session.last_panel.text
    assert not await r.worker.once() and len(r.api.messages) == 1


async def test_closed_order_with_stale_blocked_enrollment_is_not_counted_as_unpaid_problem(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    r.api.item["attributes"] = {"delivery-critical": "changed outside"}
    order = r.api.order("closed")
    Orders(r.store).ingest(order)
    await EnrollmentWorker(r.store, r.api).once()
    order["status"] = "CONFIRMED"
    Orders(r.store).ingest(order)
    await r.ui.click("attention")
    assert "order:closed" not in buttons(r.session.last_panel)
    assert not r.api.messages


async def test_needs_fulfillment_order_is_actionable_after_confirmed_delivery(shop):
    r = await shop()
    await setup_ui(r, "unique_stock", legacy_reminder=True)
    Orders(r.store).ingest(r.api.order("needs-owner"))
    await EnrollmentWorker(r.store, r.api).once()
    for _ in range(5):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "NEEDS_FULFILLMENT"}]
    sent = list(r.api.messages)
    await r.ui.click("home")
    assert "orders" in buttons(r.session.last_panel)
    await r.ui.click("orders")
    assert "attention" in buttons(r.session.last_panel)
    await r.ui.click("attention")
    assert "order:needs-owner" in buttons(r.session.last_panel)
    assert "завершение сделки" in r.session.last_panel.text
    assert r.api.messages == sent


async def test_attention_paginates_real_paid_problems_without_page_size_counter(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    r.api.item["attributes"] = {"delivery-critical": "changed"}
    for index in range(12):
        Orders(r.store).ingest(r.api.order("blocked-" + str(index)))
    for _ in range(3):
        await EnrollmentWorker(r.store, r.api).once()
    assert len(r.store.rows("SELECT 1 FROM auto_enrollment WHERE status='BLOCKED'")) == 12
    await r.ui.click("attention")
    assert "Проблемных заказов: 12" in r.session.last_panel.text
    assert len([key for key in buttons(r.session.last_panel) if key.startswith("order:")]) == 10
    assert len(r.session.last_panel.text.encode("utf-16-le")) // 2 < 4096
    await r.ui.click("attention:1")
    assert "Проблемных заказов: 12" in r.session.last_panel.text
    assert len([key for key in buttons(r.session.last_panel) if key.startswith("order:")]) == 2
    assert not r.api.messages and not r.api.remote.payments


async def test_refunded_order_with_only_unknown_automatic_completion_is_history_not_attention(shop):
    r = await shop()
    order = r.api.order("refunded-completion")
    Orders(r.store).ingest(order)
    ident = r.store.enqueue(
        "gift-fulfill:refunded-completion",
        "fulfill",
        order["id"],
        {"mode": "legacy", "gift_proof": order["id"], "actor": 7},
    )
    claim = r.store.claim(operation_id=ident)
    r.store.mark_started(claim)
    r.store.finish(ident, claim["lease_token"], "UNCERTAIN", error="RATE_LIMITED")
    await r.ui.click("attention")
    assert "order:refunded-completion" in buttons(r.session.last_panel)
    order["status"] = "ROLLED_BACK"
    Orders(r.store).ingest(order)
    await r.ui.click("attention")
    assert "order:refunded-completion" not in buttons(r.session.last_panel)
    assert "ops:list:UNCERTAIN:0" not in buttons(r.session.last_panel)
    assert r.store.rows("SELECT status,error_code FROM outbox WHERE id=?", (ident,)) == [
        {"status": "UNCERTAIN", "error_code": "RATE_LIMITED"}
    ]
    assert not r.api.remote.payments


async def test_product_readiness_does_not_stop_for_refunded_legacy_completion(shop):
    from app.services.product_readiness import ProductReadiness

    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    order = r.api.order("closed-completion")
    orders = Orders(r.store)
    orders.ingest(order)
    op = r.store.enqueue(
        "gift-fulfill:closed-completion",
        "fulfill",
        order["id"],
        {"mode": "legacy", "gift_proof": order["id"], "actor": 7},
    )
    claim = r.store.claim(operation_id=op)
    r.store.mark_started(claim)
    r.store.finish(op, claim["lease_token"], "UNCERTAIN", error="RATE_LIMITED")
    readiness = ProductReadiness(r.store, r.api)
    active = readiness.snapshot("lot", item=r.api.item)
    assert not any(p["blocking"] and p["code"] == "UNCERTAIN" for p in active["problems"])
    order["status"] = "ROLLED_BACK"
    orders.ingest(order)
    await r.ui.click("item:lot")
    assert "Результат операции неизвестен" not in r.session.last_panel.text
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "UNCERTAIN"}]
    assert not r.api.remote.payments


async def test_closed_after_sale_message_does_not_reappear_as_product_stop(shop):
    from app.services.product_readiness import ProductReadiness

    r = await shop()
    await setup_ui(r, "unique_stock")
    order = r.api.order("old-message")
    orders = Orders(r.store)
    orders.ingest(order)
    op = r.store.enqueue("old-message", "chain_message", order["id"], {"text": "fixture"})
    claim = r.store.claim(operation_id=op)
    r.store.mark_started(claim)
    r.store.finish(op, claim["lease_token"], "UNCERTAIN", error="NETWORK_ERROR")
    order["status"] = "CONFIRMED"
    orders.ingest(order)
    readiness = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
    assert not any(p["code"] == "UNCERTAIN" for p in readiness["problems"])
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (op,)) == [{"status": "UNCERTAIN"}]
    assert not r.api.messages
