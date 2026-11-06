import pytest

from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


@pytest.mark.parametrize(
    "handler,content",
    [("unique_stock", None), ("fixed_text", "saved text"), ("telegram_gifts", "gift: heart")],
)
async def test_existing_server_listing_uses_one_code_without_copy_or_history_loss(shop, handler, content):
    r = await shop(gift=handler == "telegram_gifts")
    Orders(r.store).ingest(r.api.order("historical", status="CONFIRMED"))
    before = r.store.rows("SELECT * FROM orders WHERE id='historical'")
    await setup_ui(r, handler, content)
    product = LogicalProducts(r.store, r.api).listing("lot")
    assert len(product["code"]) == 3 and r.api.item["name"].endswith("[" + product["code"] + "]")
    assert set(r.api.items) == {"lot"}
    assert len(r.store.rows("SELECT * FROM logical_products")) == 1
    assert r.store.rows("SELECT * FROM orders WHERE id='historical'") == before
    assert not r.api.messages and not r.api.remote.payments


async def test_global_gift_stop_is_local_and_current_item_offers_enable_not_stop(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    before = len(r.api.calls)
    await r.ui.stop("gft:stop")
    assert len(r.api.calls) == before
    assert r.store.setting("gifts:stopped") is True
    assert r.store.rows("SELECT enabled FROM logical_products") == [{"enabled": 0}]
    await r.ui.click("lp:item:lot")
    shown = buttons(r.session.last_panel)
    assert any(k.endswith(":arm") for k in shown)
    assert not any(k.endswith(":stop") for k in shown)
    assert r.store.setting("gifts:stopped") is True
    assert not r.api.remote.payments


from tests.test_gifts import rig as legacy_fixture

legacy = legacy_fixture


async def test_legacy_startup_projection_redirects_all_gift_entries_to_one_product(legacy):
    from aiogram import Bot
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    from app.telegram.storage import SQLiteStorage
    from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher

    r = legacy
    await r.service.bind(7, "item", "heart")
    r.store.set_setting("playerok:account_id", r.api.account.id)
    before = r.store.rows("SELECT * FROM gift_listings")
    registry = LogicalProducts(r.store, r.api)
    assert registry.migrate_legacy() == 1 and registry.migrate_legacy() == 0
    product = registry.listing("item")
    assert product["state"] == "LEGACY"
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    dp = dispatcher(r.store, r.api)
    ui = Interaction(bot, dp)
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        for route in ("gft:item:item", "gft:bind:item", "gft:enable:item", "gft:arm:item:old:0"):
            await ui.click(route)
            assert (await state.get_data())["logical_view"]["product"] == product["id"]
            assert not any(
                k.startswith(("gft:arm:", "gft:confirmbind:")) for k in buttons(bot.session.last_panel)
            )
        assert r.store.rows("SELECT * FROM gift_listings") == before
        assert len(r.store.rows("SELECT * FROM logical_products")) == 1
        assert not r.store.rows("SELECT * FROM outbox") and not r.remote.payments
    finally:
        await dp.storage.close()
        await bot.session.close()
