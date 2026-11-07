"""Owner replacement keeps the permanent identity and requires a new gift consent."""

from copy import deepcopy

from app.gifts.core import SEED_GIFTS
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import click, setup_ui

shop = shop_fixture


async def test_replacement_edit_and_new_catalog_selection_preserve_identity_and_stay_off_until_arm(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: zombie_bear")
    logical = LogicalProducts(r.store, r.api)
    old = logical.listing("lot")
    Orders(r.store).ingest(r.api.order("old-sale", status="CONFIRMED"))
    history = r.store.rows("SELECT * FROM orders")
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":stop")
    await click(r, suffix=":stop_confirm")
    del r.api.remote.catalog["zombie_bear"]

    def update(id, **changes):
        r.api.mutations.append(deepcopy(changes))
        r.api.item.update(changes)
        return {"id": id}  # The real defect: accepted changes, incomplete mutation body.

    r.api.account.update_item = update
    ident = r.store.enqueue(
        "replace-copy",
        "edit",
        "lot",
        {"name": "Обычный мишка", "description": "Новая поставка подарка", "price": 90},
        actor=7,
    )
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": "SUCCEEDED"}]
    product = logical.listing("lot")
    assert product["id"] == old["id"] and product["code"] == old["code"]
    assert not product["enabled"] and product["verified_at"] is None
    assert r.api.item["name"].endswith("[" + old["code"] + "]")
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":configure")
    await click(r, suffix=":mode:telegram_gifts")
    await click(r, suffix=":giftclear")
    await click(r, suffix=":giftpick:bear")
    await click(r, suffix=":giftdone")
    await click(r, suffix=":preview")
    await click(r, suffix=":commit")
    await r.worker.once()
    assert not logical.listing("lot")["enabled"]
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":arm")
    assert "Включить выдачу будущих заказов" in r.session.last_panel.text, r.session.last_panel.text
    await click(r, suffix=":enable:inherit")
    product = logical.listing("lot")
    gift = r.store.rows("SELECT * FROM gift_listings WHERE item_id='lot'")[0]
    assert product["id"] == old["id"] and product["code"] == old["code"] and product["enabled"]
    assert gift["enabled"] and gift["gift_code"] == "bear" and gift["gift_id"] == str(SEED_GIFTS["bear"][1])
    assert r.store.rows("SELECT * FROM orders") == history
    assert not r.api.remote.payments and not r.api.messages
    assert not r.store.rows("SELECT * FROM gift_jobs")
