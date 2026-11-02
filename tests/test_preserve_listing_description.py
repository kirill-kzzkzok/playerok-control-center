"""Permanent marker edits use the real UI, Outbox and controlled API boundary."""

from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_unified_delivery_controls import state, stop
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


async def configured(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "private-test-unit")
    await stop(r)
    return r


async def test_normal_description_edit_preserves_code_and_confirms_real_worker_result(shop):
    r = await configured(shop)
    code = r.store.rows("SELECT code FROM logical_products")[0]["code"]
    await r.ui.click("editdesc:lot")
    assert code not in r.session.last_panel.text
    await r.ui.message("Новое описание покупателю")
    payload = (await state(r).get_data())["pending"]["payload"]
    assert payload["description"] == "Новое описание покупателю"
    assert r.api.item["name"].endswith("[" + code + "]")
    data = await state(r).get_data()
    await r.ui.click("commit:" + data["confirm_token"])
    await r.worker.once()
    assert r.api.item["description"] == payload["description"]
    assert len(r.api.mutations) == 2
    assert not await r.worker.once()
    assert not r.api.messages


async def test_foreign_marker_cannot_replace_a_permanent_product_code(shop):
    r = await configured(shop)
    await r.ui.click("editname:lot")
    await r.ui.message("Чужой код [ZZZ]")
    assert not (await state(r).get_data()).get("pending")
    assert len(r.store.rows("SELECT * FROM outbox")) == 1


async def test_external_description_change_after_opening_edit_blocks_stale_write(shop):
    r = await configured(shop)
    await r.ui.click("editdesc:lot")
    before = len(r.api.mutations)
    r.api.item["description"] = "Изменено владельцем на сайте"
    await r.ui.message("Старая версия редактирования")
    data = await state(r).get_data()
    await r.ui.click("commit:" + data["confirm_token"])
    await r.worker.once()
    assert len(r.api.mutations) == before
    assert r.store.rows("SELECT status FROM outbox ORDER BY created_at")[-1] == {"status": "PERMANENT_FAILED"}
