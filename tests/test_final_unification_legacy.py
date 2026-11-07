from aiogram import Bot

from tests.test_gifts import rig as rig_fixture
from tests.test_telegram_integration import Interaction, TelegramSession
from tests.test_telegram_transport import production_dispatcher
from tests.test_ux_redesign import buttons

rig = rig_fixture


async def test_existing_legacy_gift_opens_protected_canonical_migration(rig):
    r = rig
    r.store.set_setting("playerok:account_id", r.api.account.id)
    await r.service.refresh()
    await r.service.bind(7, "item", "heart")
    r.api.item["id"] = "other"
    await r.service.bind(7, "other", "heart")
    r.api.item["id"] = "item"
    original = r.store.rows("SELECT * FROM gift_listings")
    bot = Bot("123:" + "x" * 35, session=TelegramSession())
    ui = Interaction(bot, production_dispatcher(r.store, r.api))
    try:
        await ui.click("gft:item:item")
        assert any(k.startswith("lp:") for k in buttons(bot.session.last_panel))
        assert not any(k.startswith(("gft:bind:", "gft:enable:")) for k in buttons(bot.session.last_panel))
        assert r.store.rows("SELECT * FROM gift_listings") == original
        assert r.store.rows("SELECT state,enabled FROM logical_products") == [
            {"state": "LEGACY", "enabled": 0}
        ]
        products = r.store.rows("SELECT id,code FROM logical_products")
        assert r.store.rows("SELECT item_id FROM logical_listings") == [{"item_id": "item"}]
        ui.dp = production_dispatcher(r.store, r.api)
        await ui.click("gft:bind:item")
        assert r.store.rows("SELECT id,code FROM logical_products") == products
        assert not r.store.rows("SELECT * FROM outbox")
        assert not r.remote.payments
    finally:
        await bot.session.close()
