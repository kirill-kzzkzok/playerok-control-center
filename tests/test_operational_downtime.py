"""Downtime is not permission: only the original verified admission window counts."""

import time
from datetime import datetime, timedelta

import pytest

from app.database.store import Store
from app.gifts.worker import GiftWorker
from app.playerok.adapter import APIError
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from app.services.products import Products
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.outbox import Executor, OutboxWorker
from tests.test_logical_faults import reopen
from tests.test_playerok_delivery import configure
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


@pytest.mark.parametrize("minutes", [10, 30, 120])
async def test_verified_paid_order_after_original_arm_recovers_once_after_downtime(
    shop, monkeypatch, minutes
):
    r = await shop()
    await setup_ui(r, "unique_stock")
    order = r.api.order("during-downtime")
    original = LogicalProducts(r.store, r.api).listing("lot")
    at = time.time()
    Orders(r.store).ingest(order)
    if minutes == 120:
        read = r.api.call

        async def unavailable(method, **kwargs):
            if method == "get_deal":
                raise APIError("HTTP_503")
            return await read(method, **kwargs)

        r.api.call = unavailable
        await EnrollmentWorker(r.store, r.api).once()
        assert r.store.rows("SELECT status,error_code FROM auto_enrollment") == [
            {"status": "PENDING", "error_code": "HTTP_503"}
        ]
        assert not r.api.messages and not r.store.rows("SELECT * FROM reservations")
        reopen(r)
        r.api.call = read
    monkeypatch.setattr("app.workers.enrollment.time.time", lambda: at + minutes * 60)
    await EnrollmentWorker(r.store, r.api).once()
    for _ in range(5):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "SUCCEEDED"}]
    assert len(r.api.messages) == 2
    assert len(r.store.rows("SELECT * FROM reservations")) == 1
    Orders(r.store).ingest(order)
    assert await EnrollmentWorker(r.store, r.api).once() == 0
    assert not await r.worker.once()
    assert LogicalProducts(r.store, r.api).listing("lot")["armed_at"] == original["armed_at"]
    assert not r.api.remote.payments


@pytest.mark.parametrize("risk", ["history", "stop", "unknown-date", "fulfilled"])
async def test_downtime_does_not_authorize_historical_stopped_or_unproved_orders(shop, monkeypatch, risk):
    r = await shop()
    await setup_ui(r, "unique_stock")
    order = r.api.order("not-authorized")
    if risk == "history":
        order["created_at"] = "2020-01-01T00:00:00Z"
    elif risk == "stop":
        product = LogicalProducts(r.store, r.api).listing("lot")
        LogicalProducts(r.store, r.api).stop(7, product["id"])
    elif risk == "unknown-date":
        order.pop("created_at")
    else:
        order["status"] = "CONFIRMED"
    at = time.time()
    monkeypatch.setattr("app.workers.enrollment.time.time", lambda: at + 7200)
    Orders(r.store).ingest(order)
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    await r.worker.once()
    assert not r.api.messages and not r.store.rows("SELECT * FROM reservations")


@pytest.mark.parametrize("mode", ["telegram_gifts", "playerok"])
async def test_other_verified_delivery_modes_recover_after_two_hours(shop, monkeypatch, mode):
    r = await shop(gift=mode == "telegram_gifts")
    if mode == "telegram_gifts":
        await setup_ui(r, mode, "gift: heart")
    else:
        await configure(r, reply=True, fulfill=False)
    order = r.api.order("late-sale")
    if mode == "playerok":
        order["is_automated"] = True
    at = time.time()
    monkeypatch.setattr("app.workers.enrollment.time.time", lambda: at + 7200)
    Orders(r.store).ingest(order)
    for _ in range(5):
        await EnrollmentWorker(r.store, r.api).once()
        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert len(r.api.remote.payments) == int(mode == "telegram_gifts")
    assert len(r.api.messages) == 1
    assert not r.store.rows("SELECT * FROM reservations")


async def test_unknown_gift_after_downtime_remains_quarantined_after_restart(shop, monkeypatch):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    order = r.api.order("unknown-late-sale")
    at = time.time()
    monkeypatch.setattr("app.workers.enrollment.time.time", lambda: at + 7200)
    Orders(r.store).ingest(order)
    r.api.remote.pay_error = TimeoutError()
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert len(r.api.remote.payments) == 1
    for _ in range(2):
        path = r.store.path
        r.store.close()
        r.store = Store(path)
        r.worker = OutboxWorker(r.store, Executor(r.store, r.api, Products(r.store, r.api)))
        Orders(r.store).ingest(order)
        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
        await r.worker.once()
        assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "UNCERTAIN"}]
        assert len(r.api.remote.payments) == 1
        assert not r.api.messages


async def test_missing_server_date_can_be_completed_without_rearming(shop, monkeypatch):
    r = await shop()
    await setup_ui(r, "fixed_text", "private-unit")
    complete = r.api.order("partial")
    Orders(r.store).ingest({**complete, "created_at": None})
    worker = EnrollmentWorker(r.store, r.api)
    assert await worker.once() == 0
    at = time.time()
    monkeypatch.setattr("app.workers.enrollment.time.time", lambda: at + 7200)
    Orders(r.store).ingest(complete)
    assert await EnrollmentWorker(r.store, r.api).once() == 1
    for _ in range(5):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert [m["text"] for m in r.api.messages].count("private-unit") == 1


async def test_conflicting_server_dates_require_review_even_when_both_after_arm(shop, monkeypatch):
    r = await shop()
    await setup_ui(r, "unique_stock")
    order = r.api.order("conflicting-date")
    Orders(r.store).ingest(order)
    later = datetime.fromisoformat(order["created_at"]) + timedelta(seconds=1)
    Orders(r.store).ingest({**order, "created_at": later.isoformat()})
    at = time.time()
    monkeypatch.setattr("app.workers.enrollment.time.time", lambda: at + 7200)
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    await r.worker.once()
    assert not r.api.messages and not r.store.rows("SELECT * FROM reservations")
