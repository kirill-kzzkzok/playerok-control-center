"""Explicit owner refresh and independent readback use production paths only."""

import copy

import pytest
from aiogram import Bot

from app.database.store import Store, now
from app.playerok.adapter import APIError
from app.services.logical_products import LogicalProducts
from app.services.products import Products
from app.workers.logical_products import LogicalProductWorker
from app.workers.outbox import Executor, OutboxWorker
from tests.test_logical_products import logical as logical_fixture
from tests.test_telegram_integration import CatalogAPI, Interaction, TelegramSession
from tests.test_telegram_transport import production_dispatcher

logical = logical_fixture


@pytest.mark.parametrize("fail_last", [False, "network", "timeout"])
async def test_refresh_traverses_all_pages_before_replacing_snapshot(tmp_path, fail_last):
    store = Store(tmp_path / "refresh.sqlite")
    api, session = CatalogAPI(), TelegramSession()
    store.set_setting("playerok:account_id", "seller")
    key = "ui:catalog:seller"
    old = {"at": now(), "complete": True, "background": True, "items": [], "cursor": None}
    store.set_setting(key, old)
    items = [
        {
            "id": f"lot-{i}",
            "name": "New target" if i == 100 else f"Lot {i}",
            "price": 90,
            "status": "APPROVED",
            "user": {"id": "seller"},
        }
        for i in range(101)
    ]
    reads = []
    original = api.call

    async def pages(method, **kwargs):
        if method != "get_my_items":
            return await original(method, **kwargs)
        start = int(kwargs.get("after_cursor") or 0)
        reads.append(start)
        if fail_last and start >= 96:
            if fail_last == "timeout":
                raise TimeoutError
            raise APIError("NETWORK_ERROR")
        end = min(start + kwargs["count"], len(items))
        return {
            "items": items[start:end],
            "page_info": {
                "has_next_page": end < len(items),
                "end_cursor": str(end) if end < len(items) else None,
            },
        }

    api.call = pages
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, production_dispatcher(store, api))
    try:
        await ui.message("/start")
        await ui.click("items")
        await ui.click("il:search")
        await ui.message("New target")
        await ui.click("il:refreshsearch")
        assert reads == [0, 24, 48, 72, 96]
        snapshot = store.setting(key)
        if fail_last:
            assert snapshot == old
            pending = store.setting(key + ":pending")
            assert len(pending["scan"]["items"]) == 96
            # Existing background mechanism resumes the validated traversal.
            from app.services.item_filters import refresh_due_catalog

            fail_last = False
            store.set_setting(key + ":pending", {**pending, "due": 0})
            await refresh_due_catalog(store, api)
            assert store.setting(key)["complete"]
            assert len(store.setting(key)["items"]) == 101
        else:
            assert snapshot["complete"] and len(snapshot["items"]) == 101
            assert "item:lot-100" in str(session.last_panel.reply_markup)
            assert "Поиск не завершён" not in session.last_panel.text
            assert "Совпало: 1" in session.last_panel.text
        assert not store.rows("SELECT * FROM outbox")
    finally:
        await ui.dp.storage.close()
        await session.close()
        store.close()


@pytest.mark.parametrize("restart", [False, True])
async def test_delayed_binding_reconciliation_precedes_unrelated_failed_scan(logical, restart):
    store, api, service = logical
    prepared = await service.prepare(7, "lot", "manual", {})
    saved = service.commit(7, prepared)
    before = copy.deepcopy(api.item)
    update = api.account.update_item

    def stale_ack(**kwargs):
        update(**kwargs)
        return copy.deepcopy(before)

    original = api.call
    stale = True

    async def read(method, **kwargs):
        if stale and api.mutations:
            return copy.deepcopy(before)
        return await original(method, **kwargs)

    api.account.update_item = stale_ack
    api.call = read
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
    if restart:
        path = store.path
        store.close()
        store = Store(path)
        service = LogicalProducts(store, api)
    stale = False
    worker = LogicalProductWorker(store, api)

    async def failed_scan(**kwargs):
        raise APIError("NETWORK_ERROR")

    worker.scan = failed_scan
    try:
        with pytest.raises(APIError, match="NETWORK_ERROR"):
            await worker.once()
        assert store.rows("SELECT status FROM outbox") == [{"status": "SUCCEEDED"}]
        assert service.product(saved["product_id"])["state"] == "VERIFIED"
        assert not service.product(saved["product_id"])["enabled"]
        assert len(api.mutations) == 1
    finally:
        if restart:
            store.close()
