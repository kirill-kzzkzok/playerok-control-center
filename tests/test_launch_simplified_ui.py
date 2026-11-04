"""A confirmed new draft reaches final launch without a manual check click."""

from tests.test_final_modern_gift_creation import shop as shop_fixture
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_gift_launch_prepares_automatically_but_does_not_publish_or_pay(shop):
    r = shop
    await r.create_and_prepare(prepare_launch=False)
    await r.ui.click("launch:from:" + r.original_draft)
    shown = buttons(r.session.last_panel)
    assert any(key.endswith(":commit") for key in shown)
    assert not any(key.endswith((":check", ":inherit", ":complete", ":refund")) for key in shown)
    assert any(key.endswith(":advanced") for key in shown)
    assert not r.api.published and not r.remote.payments
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind IN ('publish','premium_publish','gift_send')")
    await r.launch_action("advanced")
    assert any(key.endswith(":complete") for key in buttons(r.session.last_panel))


async def test_cached_preparation_does_not_show_launch_after_explicit_gift_stop(shop):
    r = shop
    await r.create_and_prepare(prepare_launch=False)
    await r.ui.click("launch:from:" + r.original_draft)
    assert any(key.endswith(":commit") for key in buttons(r.session.last_panel))
    ident = (await r.state.get_data())["launch"]["id"]
    r.service.stop(7)
    await r.ui.click("launch:open:" + ident)
    assert not any(key.endswith(":commit") for key in buttons(r.session.last_panel))
    assert "остановлена" in r.session.last_panel.text
    assert not r.api.published and not r.remote.payments


async def test_automatic_preparation_final_button_queues_one_publication(shop):
    r = shop
    await r.create_and_prepare(prepare_launch=False)
    await r.ui.click("launch:from:" + r.original_draft)
    commit = next(key for key in buttons(r.session.last_panel) if key.endswith(":commit"))
    await r.ui.click(commit)
    await r.ui.click(commit)
    assert r.store.rows("SELECT status FROM outbox WHERE kind='publish'") == [{"status": "PENDING"}]
    assert not r.api.published and not r.remote.payments
    await r.worker.once()
    assert len(r.api.published) == 1
    assert r.store.rows("SELECT status FROM outbox WHERE kind='publish'") == [{"status": "SUCCEEDED"}]
    assert not r.remote.payments
