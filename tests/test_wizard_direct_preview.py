"""Content selection leads to preview, never an extra consent or a publication."""

import pytest

from tests.test_final_modern_gift_creation import shop as shop_fixture
from tests.test_ux_redesign import buttons

shop = shop_fixture


@pytest.mark.parametrize("mode,index", [("manual", 0), ("text", 1), ("stock", 2), ("gifts", 3)])
async def test_content_is_followed_by_preview_without_empty_confirmation(shop, mode, index):
    r = shop
    await r.create_and_prepare(stop_before_delivery=True, description="Описание покупателю")
    await r.wizard_action(f"choose:{index}")
    if mode in {"text", "stock"}:
        await r.ui.message("private-delivery-data")
    if mode == "gifts":
        await r.wizard_action("gift_add:heart")
        await r.wizard_action("gift_done")
    draft = r.store.get_draft((await r.state.get_data())["draft_id"], 7)
    assert draft["payload"]["step"] == "preview"
    shown = buttons(r.session.last_panel)
    assert any(key.endswith(":submit") for key in shown)
    assert not any(key.endswith(":confirm_done") for key in shown)
    assert "private-delivery-data" not in r.session.last_panel.text
    assert not r.store.rows("SELECT * FROM outbox")
    assert not r.store.rows("SELECT * FROM logical_products")
    assert not r.api.created and not r.api.published and not r.remote.payments
