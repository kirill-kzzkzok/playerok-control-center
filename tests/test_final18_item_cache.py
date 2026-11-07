import asyncio
from copy import deepcopy
from time import perf_counter

from app.services.product_readiness import ProductReadiness
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_confirmed_edit_card_is_immediate_without_warm_post_edit_cache(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    readiness = ProductReadiness(r.store, r.api)
    old = deepcopy(r.api.item)
    old_version = readiness.item_version("lot")
    await r.ui.click("editname:lot")
    await r.ui.message("Fresh confirmed title")
    commit = next(k for k in buttons(r.session.last_panel) if k.startswith("commit:"))
    await r.ui.click(commit)
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox WHERE kind='edit' ORDER BY created_at DESC LIMIT 1") == [
        {"status": "SUCCEEDED"}
    ]
    original = r.api.call
    calls = []

    async def slow(method, **kwargs):
        calls.append(method)
        await asyncio.sleep(6)
        return await original(method, **kwargs)

    r.api.call = slow
    for restart in (False, True):
        if restart:
            r.api.readiness_items = {}
        start = perf_counter()
        await asyncio.wait_for(r.ui.click("item:lot"), 0.7)
        assert perf_counter() - start < 0.7 and not calls
        assert "Fresh confirmed title" in r.session.last_panel.text
    readiness.remember_item(old, version=old_version)
    await r.ui.click("item:lot")
    assert "Fresh confirmed title" in r.session.last_panel.text
    assert r.store.setting("readiness:lot")["ui_requested"] is True
    assert not r.api.messages and not r.api.remote.payments


async def test_first_item_read_cannot_stamp_a_late_old_response_with_the_new_edit_version(shop):
    from app.playerok.adapter import APIError

    r = await shop()
    readiness = ProductReadiness(r.store, r.api)
    original = r.api.call
    entered, release = asyncio.Event(), asyncio.Event()
    first = True

    async def delayed(method, **kwargs):
        nonlocal first
        value = await original(method, **kwargs)
        if method == "get_item" and first:
            first = False
            entered.set()
            await release.wait()
        return value

    r.api.call = delayed
    reading = asyncio.create_task(readiness.ui_item(7, "lot"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await r.ui.click("editname:lot")
        await r.ui.message("Newer confirmed title")
        commit = next(k for k in buttons(r.session.last_panel) if k.startswith("commit:"))
        await r.ui.click(commit)
        await r.worker.once()
        assert r.store.rows("SELECT status FROM outbox WHERE kind='edit'") == [{"status": "SUCCEEDED"}]
        release.set()
        try:
            await reading
        except APIError as error:
            assert error.read_only and error.code == "ITEM_READ_SUPERSEDED"
        await r.ui.click("item:lot")
        assert "Newer confirmed title" in r.session.last_panel.text
        assert len(r.api.mutations) == 1
        assert not r.api.messages and not r.api.remote.payments
    finally:
        release.set()
        if not reading.done():
            await reading
