"""The main wizard reaches the same guarded launch for every supported mode."""

from copy import deepcopy

import pytest

from app.services.logical_products import LogicalProducts
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_final_modern_gift_creation import shop as shop_fixture
from tests.test_gifts import Marketplace

shop = shop_fixture


@pytest.mark.parametrize("mode,index", [("manual", 0), ("text", 1), ("stock", 2), ("gifts", 3)])
async def test_clean_wizard_creation_launch_and_immediate_first_sale(shop, mode, index):
    r = shop
    await r.create_and_prepare(stop_before_delivery=True, description="Обычное описание товара покупателю")
    await r.wizard_action(f"choose:{index}")
    if mode in {"text", "stock"}:
        await r.ui.message("private-delivery-unit")
    if mode == "gifts":
        await r.wizard_action("gift_add:heart")
        await r.wizard_action("gift_done")
    await r.wizard_action("confirm_done")
    draft = (await r.state.get_data())["draft_id"]
    await r.wizard_action("submit")
    await r.worker.once()
    assert len(r.api.created) == 1 and r.api.item["status"] == "DRAFT"
    assert "gift:" not in r.api.item["description"]

    def publish(**kwargs):
        assert kwargs["item_id"] == "item"
        product = LogicalProducts(r.store, r.api).listing("item")
        if mode != "manual":
            assert product["enabled"] and product["armed_at"]
        r.api.published.append(kwargs)
        r.api.item["status"] = "APPROVED"
        r.api.add_order("immediate", username="@kzzkzok")
        return deepcopy(r.api.item)

    r.api.account.publish_item = publish
    r.api.account.send_message = lambda chat_id, text: Marketplace.send_message(r.api, chat_id, text)
    await r.ui.click("launch:from:" + draft)
    await r.launch_action("check")
    await r.launch_action("commit")
    await r.worker.once()
    assert len(r.api.published) == 1 and r.api.item["status"] == "APPROVED"
    await r.sync.once()
    for _ in range(5):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await r.gifts.once()
        await r.worker.once()
    if mode == "gifts":
        assert len(r.remote.payments) == 1
        assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
    elif mode != "manual":
        assert [text for _, text in r.api.sent] == ["private-delivery-unit"]
    else:
        assert not r.api.sent and not r.store.rows("SELECT * FROM reservations")
    if mode != "gifts":
        assert not r.remote.payments
    assert not await r.worker.once()
