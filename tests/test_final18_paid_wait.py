from datetime import UTC, datetime, timedelta

import pytest

from app.services.orders import Orders
from app.telegram.home_panel import HomePanel
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_product_readiness import shop as shop_fixture
from tests.test_telegram_transport import production_dispatcher
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


@pytest.mark.parametrize("mode,value", [("unique_stock", None), ("fixed_text", "private-unit")])
async def test_paid_incomplete_wait_is_visible_then_recovers_once_without_clicks(shop, mode, value):
    r = await shop()
    await setup_ui(r, mode, value)
    order = r.api.order("waiting")
    order.pop("has_problem")
    Orders(r.store).ingest(order)
    await EnrollmentWorker(r.store, r.api).once()
    assert r.store.rows("SELECT status FROM auto_enrollment") == [{"status": "PENDING"}]
    assert not r.store.rows("SELECT * FROM reservations") and not r.api.messages
    assert not HomePanel(r.store, r.api).paid_issues()
    earlier = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    r.store.execute("UPDATE inbox SET received_at=? WHERE key='order:waiting:PAID'", (earlier,))
    issues = HomePanel(r.store, r.api).paid_issues()
    assert len(issues) == 1 and issues[0]["enrollment_wait"] == 1
    await r.ui.click("home")
    assert "attention" in buttons(r.session.last_panel)
    await r.ui.click("attention")
    assert "автоматическ" in r.session.last_panel.text and "5 мин" in r.session.last_panel.text
    assert "Следующая проверка:" in r.session.last_panel.text
    assert "order:waiting" in buttons(r.session.last_panel)
    await r.ui.click("order:waiting")
    assert "Следующая проверка:" in r.session.last_panel.text
    assert "Ручное действие пока не требуется" in r.session.last_panel.text
    r.ui.dp = production_dispatcher(r.store, r.api)
    r.api.orders["waiting"]["has_problem"] = False
    r.store.execute("UPDATE auto_enrollment SET due_at=0")
    await EnrollmentWorker(r.store, r.api).once()
    for _ in range(10):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "SUCCEEDED"}]
    assert not HomePanel(r.store, r.api).paid_issues()
    before = list(r.api.messages)
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    await r.worker.once()
    assert len(before) == 2 and r.api.messages == before
    assert not r.api.remote.payments
