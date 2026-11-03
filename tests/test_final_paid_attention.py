"""Attention is driven by paid obligations, not notification delivery."""

from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_blocked_paid_order_is_visible_even_if_notification_is_missing(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    r.api.item["description"] = "Внешнее критическое изменение описания"
    Orders(r.store).ingest(r.api.order("blocked-paid"))
    await EnrollmentWorker(r.store, r.api).once()
    assert r.store.rows("SELECT status FROM auto_enrollment") == [{"status": "BLOCKED"}]
    assert not r.store.rows("SELECT * FROM notifications")
    await r.ui.click("home")
    assert "attention" in buttons(r.session.last_panel)
    await r.ui.click("attention")
    assert "order:blocked-paid" in buttons(r.session.last_panel)
    assert "1" in r.session.last_panel.text
    await r.ui.click("order:blocked-paid")
    assert "Статус: Оплачен" in r.session.last_panel.text
    assert "Изменилось описание" in r.session.last_panel.text
    assert not r.api.messages and not r.api.remote.payments


async def test_closed_order_with_stale_blocked_enrollment_is_not_counted_as_unpaid_problem(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    r.api.item["description"] = "Изменено снаружи"
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
    await setup_ui(r, "unique_stock", completion=True)
    Orders(r.store).ingest(r.api.order("needs-owner"))
    await EnrollmentWorker(r.store, r.api).once()
    for _ in range(5):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "NEEDS_FULFILLMENT"}]
    sent = list(r.api.messages)
    await r.ui.click("home")
    assert "attention" in buttons(r.session.last_panel)
    await r.ui.click("attention")
    assert "order:needs-owner" in buttons(r.session.last_panel)
    assert "завершение сделки" in r.session.last_panel.text
    assert r.api.messages == sent


async def test_attention_paginates_real_paid_problems_without_page_size_counter(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    r.api.item["description"] = "Изменение критического описания"
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
