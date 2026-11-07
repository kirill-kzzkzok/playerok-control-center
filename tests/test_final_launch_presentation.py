"""One launch preview describes actual message timing for the chosen mode."""

from tests.test_final_modern_gift_creation import shop as shop_fixture
from tests.test_final_platform_creation import platform_draft

shop = shop_fixture


async def test_platform_launch_does_not_advertise_bot_delivery_messages(shop):
    r = shop
    await platform_draft(r)
    text = r.session.last_panel.text
    assert "Приветствие:" not in text and "После выдачи:" not in text
    assert "Выдаёт Playerok" in text
    ident = (await r.state.get_data())["launch"]["id"]
    value = r.store.get_draft(ident, 7)["payload"]
    assert value["mode"] == "playerok" and value["platform_reply"] is False
    assert not r.api.published and not r.remote.payments
    assert not r.store.rows(
        "SELECT 1 FROM outbox WHERE kind IN ('publish','premium_publish','delivery','delivery_text')"
    )


async def test_gift_launch_default_notification_is_explicit(shop):
    r = shop
    await r.create_and_prepare(prepare_launch=False)
    await r.ui.click("launch:from:" + r.original_draft)
    text = r.session.last_panel.text
    assert "Приветствие:" not in text
    assert "Выдача: Telegram Gifts" in text
    assert "стандартное /" not in text
    ident = (await r.state.get_data())["launch"]["id"]
    value = r.store.get_draft(ident, 7)["payload"]
    assert value["mode"] == "gifts" and value["after"] == ""
    assert not r.api.published and not r.remote.payments
