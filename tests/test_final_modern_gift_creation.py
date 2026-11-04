"""New gifts have one logical identity even when old metadata was pasted."""

import json
from copy import deepcopy

import pytest

from app.services.logical_products import LogicalProducts
from tests.test_final_acceptance_e2e import acceptance as fixture
from tests.test_ux_redesign import buttons


@pytest.fixture
async def shop(tmp_path, monkeypatch):
    async for r in fixture.__wrapped__(tmp_path, monkeypatch):
        yield r


async def test_pasted_legacy_tag_cannot_create_a_second_gift_identity_path(shop):
    r = shop
    await r.create_and_prepare(prepare_launch=False)
    product = LogicalProducts(r.store, r.api).listing("item")
    assert product is not None
    assert product["state"] == "VERIFIED" and not product["enabled"]
    assert json.loads(product["config"])["code"] == "heart"
    assert "gift:" not in r.api.item["description"].lower()
    assert r.api.item["name"].endswith("[" + product["code"] + "]")
    assert len(r.store.rows("SELECT * FROM logical_products")) == 1
    assert not r.api.published and not r.remote.payments
    assert not r.store.rows("SELECT * FROM gift_listings WHERE enabled=1")


async def test_editing_description_cannot_silently_change_the_selected_gift(shop):
    r = shop
    await r.create_and_prepare(stop_before_delivery=True)
    await r.wizard_action("choose:3")
    await r.wizard_action("confirm_done")
    ident = (await r.state.get_data())["draft_id"]
    before = r.store.get_draft(ident, 7)["payload"]
    assert before["gift_code"] == "heart" and "gift:" not in before["description"]
    await r.wizard_action("edit:description")
    await r.ui.message("Новое описание покупателю\ngift: rose")
    after = r.store.get_draft(ident, 7)["payload"]
    assert after["gift_code"] == before["gift_code"]
    assert after["description"] == before["description"]
    assert "не совпадает" in r.session.calls[-1].text
    assert not r.api.created and not r.remote.payments
    assert not r.store.rows("SELECT * FROM gift_listings WHERE enabled=1")


async def test_unbound_server_draft_gift_selection_cannot_write_another_legacy_tag(shop):
    r = shop
    # Existing server DRAFT without a logical identity: use its real old launch entry.
    await r.ui.message("/start")
    from app.services.account_binding import bind_account

    bind_account(r.store, r.api.account.id)
    await r.sync.once()
    await r.ui.click("launch:item:item")
    await r.launch_action("catalog")
    await r.launch_action("gift:heart")
    await r.launch_action("savegifts")
    queued = r.store.rows("SELECT payload FROM outbox WHERE kind='edit'")
    assert queued or (await r.state.get_data()).get("logical_prepared"), r.session.calls[-1].text
    assert not any("gift:" in json.loads(row["payload"]).get("description", "").lower() for row in queued)
    calls = []

    def update(id, **changes):
        assert id == r.api.item["id"]
        calls.append(deepcopy(changes))
        r.api.item.update(changes)
        return deepcopy(r.api.item)

    r.api.account.update_item = update
    confirm = next(
        key for key in buttons(r.session.calls[-1]) if key.startswith("lp:") and key.endswith(":commit")
    )
    await r.ui.click(confirm)
    await r.worker.once()
    await r.ui.click(confirm)
    assert not await r.worker.once()
    product = LogicalProducts(r.store, r.api).listing("item")
    assert product["state"] == "VERIFIED" and not product["enabled"]
    assert len(calls) == 1 and "gift:" not in r.api.item["description"].lower()
    assert not r.api.published and not r.remote.payments


async def test_gift_picker_for_a_new_listing_opens_the_modern_catalog(shop):
    r = shop
    await r.ui.message("/start")
    from app.services.account_binding import bind_account

    bind_account(r.store, r.api.account.id)
    await r.sync.once()
    await r.ui.click("gft:choose")
    await r.ui.click("gft:item:item")
    data = await r.state.get_data()
    assert data.get("logical_setup", {}).get("handler") == "telegram_gifts"
    assert any(":giftpick:heart" in key for key in buttons(r.session.calls[-1]))
    assert not r.store.rows("SELECT * FROM gift_listings")
    assert not r.store.rows("SELECT * FROM outbox") and not r.remote.payments


async def test_new_modern_gift_draft_is_visible_before_arming(shop):
    r = shop
    await r.create_and_prepare(prepare_launch=False)
    assert not r.store.rows("SELECT * FROM gift_listings")
    await r.ui.click("gft:bindings")
    assert "gft:item:item" in buttons(r.session.calls[-1])
    await r.ui.click("gft:item:item")
    assert "logical_view" in await r.state.get_data()
    product = LogicalProducts(r.store, r.api).listing("item")
    assert "[" + product["code"] + "]" in r.session.calls[-1].text
    assert not r.api.published and not r.remote.payments


async def test_old_unbound_gift_confirmation_cannot_create_a_competing_binding(shop):
    r = shop
    await r.ui.message("/start")
    await r.state.set_data({"gift_bind": {"item": "item", "nonce": "old-confirmation"}})
    await r.ui.click("gft:confirmbind:old-confirmation")
    assert "Старое подтверждение" in r.session.calls[-1].text
    assert not r.store.rows("SELECT * FROM gift_listings") and not r.remote.payments
