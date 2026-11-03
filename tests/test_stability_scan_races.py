"""Shared scan entry points and stale-result fencing after real owner controls."""

import asyncio

import pytest

from app.services.logical_products import LogicalProducts
from app.workers.logical_products import LogicalProductWorker
from tests.test_stability_deal_completeness import shop as shop_fixture
from tests.test_universal_delivery_e2e import click, setup_ui


@pytest.fixture
async def shop(tmp_path):
    async for create in shop_fixture.__wrapped__(tmp_path):
        yield create


async def test_manual_and_background_scan_share_one_lock(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "private")
    product = LogicalProducts(r.store, r.api).listing("lot")
    entered, release = asyncio.Event(), asyncio.Event()
    original = r.api.call
    lists = []

    async def call(method, **kwargs):
        if method == "get_my_items":
            lists.append(method)
            if len(lists) == 1:
                entered.set()
                await release.wait()
        return await original(method, **kwargs)

    r.api.call = call
    background = asyncio.create_task(LogicalProductWorker(r.store, r.api).once())
    await asyncio.wait_for(entered.wait(), 5)
    manual = asyncio.create_task(LogicalProductWorker(r.store, r.api).scan(product_id=product["id"]))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    before = len(lists)
    release.set()
    await asyncio.gather(background, manual)
    assert before == 1, "Manual scan bypassed the background scan lock"
    assert not r.api.messages and not r.api.remote.payments


async def test_error_from_scan_before_owner_stop_is_discarded(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "private")
    service = LogicalProducts(r.store, r.api)
    old = dict(service.product(service.listing("lot")["id"]))
    await click(r, suffix=":stop")
    assert service.product(old["id"])["control_revision"] != old["control_revision"]
    LogicalProductWorker(r.store, r.api).problem(old, "RELIST_CONFIRMATION_REQUIRED")
    assert service.product(old["id"])["error_code"] is None
    assert not r.store.rows("SELECT 1 FROM inbox WHERE kind='logical_product_blocked'")


async def test_scan_success_before_stop_cannot_refresh_listing_confirmation(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "private")
    service = LogicalProducts(r.store, r.api)
    old = service.listing("lot")
    entered, release = asyncio.Event(), asyncio.Event()
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_my_items":
            entered.set()
            await release.wait()
        return await original(method, **kwargs)

    r.api.call = call
    scanning = asyncio.create_task(LogicalProductWorker(r.store, r.api).once())
    await asyncio.wait_for(entered.wait(), 5)
    await click(r, suffix=":stop")
    release.set()
    await scanning
    current = service.listing("lot")
    assert not current["enabled"]
    assert current["verified_at"] == old["verified_at"]
    assert current["error_code"] is None
    assert not r.api.messages and not r.api.remote.payments
