from app.services.orders import Orders
from app.workers.enrollment import EnrollmentWorker
from tests.test_product_readiness import shop as shop_fixture
from tests.test_targeted_ux import financial as financial_fixture
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture
financial = financial_fixture


async def test_maintenance_delivery_labels_describe_seller_results(shop):
    r = await shop()
    for route in ("tools", "tools:delivery"):
        await r.ui.click(route)
        content = r.session.last_panel.text + " ".join(buttons(r.session.last_panel).values())
        for jargon in (
            "Выдача и цепочки",
            "Цепочки выдачи",
            "Автовыдача новых заказов",
            "Все настройки выдачи",
            "Выдано — ожидает подтверждения",
        ):
            assert jargon not in content
    assert not r.store.rows("SELECT * FROM outbox")


async def test_paid_conflict_has_exact_direct_attention_badge_on_home(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    r.api.item["attributes"] = {"delivery-critical": "changed"}
    Orders(r.store).ingest(r.api.order("blocked-paid"))
    await EnrollmentWorker(r.store, r.api).once()
    await r.ui.click("home")
    assert buttons(r.session.last_panel).get("attention") == "⚠️ Требуют действия: 1"
    await r.ui.click("attention")
    assert "order:blocked-paid" in buttons(r.session.last_panel)
    assert not r.api.messages and not r.api.remote.payments


async def test_price_screen_shows_current_discount_without_unsupported_base_mutation(financial):
    r = financial
    r.api.item.update(raw_price=1000, price=900)
    await r.ui.message("/start")
    await r.ui.click("discount:item")
    shown = r.session.last_panel.text
    assert "Базовая цена: 1000 ₽" in shown
    assert "Текущая цена: 900 ₽" in shown
    assert "Скидка: 10.00%" in shown
    assert "Базовая цена не изменяется" in shown
    assert not r.store.rows("SELECT * FROM outbox")


async def test_invalid_price_input_keeps_contextual_return_in_same_message(financial):
    r = financial
    r.api.item.update(raw_price=1000, price=900)
    await r.ui.message("/start")
    await r.ui.click("item:item")
    await r.ui.click("iv:edit:item")
    await r.ui.click("discount:item")
    ident = r.session.sequence
    await r.ui.message("not a price")
    routes = buttons(r.session.last_panel)
    assert any(key in routes for key in ("iv:edit:item", "item:item", "discount:item"))
    assert r.session.sequence == ident
    assert not r.store.rows("SELECT * FROM outbox")


async def test_permission_help_explains_unavailable_business_actions_and_no_fake_toggle(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    r.api.account.writes_enabled = False
    await r.ui.click("rd:permissions:lot")
    text = r.session.last_panel.text.lower()
    assert "изменять лоты" in text and "из бота" in text
    assert "playerok_writes_enabled" in text and "перезапустите" in text
    assert not r.api.account.writes_enabled
    assert not r.api.messages and not r.api.remote.payments
