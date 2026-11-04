"""Standing title-only permission, real enrollment/execution, API boundary fakes."""

import asyncio
import sqlite3

import pytest

from app.database.store import Conflict
from app.gifts.worker import GiftWorker
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.logical_products import LogicalProductWorker
from tests.test_stage_a_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


@pytest.mark.parametrize(
    "handler,value",
    [("telegram_gifts", "gift: heart"), ("fixed_text", "unique-text"), ("unique_stock", None)],
)
async def test_paid_worker_recovers_title_without_owner_check_or_confirmation(shop, handler, value):
    r = await shop(gift=handler == "telegram_gifts")
    await setup_ui(r, handler, value)
    service = LogicalProducts(r.store, r.api)
    old = service.listing("lot")
    r.api.item["name"] = "Обновлённое название " + r.api.item["name"]
    Orders(r.store).ingest(r.api.order("renamed-new-order"))
    for _ in range(6):
        if handler == "telegram_gifts":
            await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
        else:
            await EnrollmentWorker(r.store, r.api).once()
            ChainWorker(r.store, r.api).once()
        await r.worker.once()
    current = service.listing("lot")
    assert current["title_snapshot"] == r.api.item["name"]
    for key in ("id", "code", "config", "armed_at", "enabled", "revision", "control_revision"):
        assert current[key] == old[key]
    if handler == "telegram_gifts":
        assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
        assert len(r.api.remote.payments) == 1
    else:
        assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "SUCCEEDED"}]
        assert len([m for m in r.api.messages if m["text"] != "Спасибо за покупку!"]) == 1


async def test_background_scan_and_owner_card_accept_only_safe_title_without_new_buttons(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    r.api.item["name"] = "Новое название " + r.api.item["name"]
    await LogicalProductWorker(r.store, r.api).once()
    assert not LogicalProducts(r.store, r.api).listing("lot")["error_code"]
    await r.ui.click("item:lot")
    assert "Изменилось название" not in r.session.last_panel.text
    assert not any("title" in key for key in buttons(r.session.last_panel))
    assert not r.api.remote.payments


async def test_concurrent_title_observations_do_not_turn_safe_rename_into_a_conflict(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    r.api.item["name"] = "Новое имя " + r.api.item["name"]
    entered, release = asyncio.Event(), asyncio.Event()
    original = r.api.call

    async def gated(method, **kwargs):
        if method == "get_item":
            entered.set()
            await release.wait()
        return await original(method, **kwargs)

    r.api.call = gated
    service = LogicalProducts(r.store, r.api)
    first = asyncio.create_task(service.refresh_title(r.api.item))
    await asyncio.wait_for(entered.wait(), 5)
    second = asyncio.create_task(service.refresh_title(r.api.item))
    await asyncio.sleep(0)
    release.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert not any(isinstance(value, BaseException) for value in outcomes), outcomes
    assert outcomes.count(True) == 1
    assert service.listing("lot")["enabled"]
    assert not r.api.remote.payments


@pytest.mark.parametrize("revoke", [True, False])
async def test_automatic_title_refresh_rechecks_authority_after_read(shop, revoke):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    service = LogicalProducts(r.store, r.api)
    before = service.listing("lot")["title_snapshot"]
    r.api.item["name"] = "Новый заголовок " + r.api.item["name"]
    original = r.api.call

    async def revoke_after_read(method, **kwargs):
        value = await original(method, **kwargs)
        if revoke:
            r.store.execute("DELETE FROM administrators WHERE user_id=7")
        else:
            r.api.account.id = "other-account"
        return value

    r.api.call = revoke_after_read
    # Owner rows referenced by durable drafts cannot be deleted: the FK itself
    # fences this attempted revocation. Runtime account change reaches authority().
    with pytest.raises(sqlite3.IntegrityError if revoke else Conflict):
        await service.refresh_title(r.api.item)
    assert service.listing("lot")["title_snapshot"] == before
    assert not r.api.remote.payments


async def test_automatic_metadata_refresh_never_lifts_explicit_owner_stop(shop):
    from tests.test_universal_delivery_e2e import click

    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    await r.ui.click("lp:item:lot")
    await click(r, suffix=":stop")
    r.api.item["name"] = "Новое имя " + r.api.item["name"]
    await r.ui.click("item:lot")
    assert not LogicalProducts(r.store, r.api).listing("lot")["enabled"]
    assert not r.store.rows("SELECT 1 FROM gift_listings WHERE enabled=1")
    Orders(r.store).ingest(r.api.order("stopped-new-order"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert not r.api.remote.payments
