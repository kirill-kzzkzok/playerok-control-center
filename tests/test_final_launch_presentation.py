"""One launch preview describes actual message timing for the chosen mode."""

from tests.test_final_modern_gift_creation import shop as shop_fixture
from tests.test_final_platform_creation import platform_draft

shop = shop_fixture


async def test_platform_launch_does_not_advertise_bot_delivery_messages(shop):
    r = shop
    await platform_draft(r)
    text = r.session.last_panel.text
    assert "Приветствие:" not in text and "После выдачи:" not in text
    assert "Сообщение после оплаты: выключено" in text
    assert "Бот не отправляет товар повторно" in text


async def test_gift_launch_default_notification_is_explicit(shop):
    r = shop
    await r.create_and_prepare(prepare_launch=False)
    await r.ui.click("launch:from:" + r.original_draft)
    text = r.session.last_panel.text
    assert "Приветствие:" not in text
    assert "После выдачи: стандартное уведомление" in text
    assert "стандартное /" not in text
