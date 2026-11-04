"""Production polling cadence includes transport time without weakening 429."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.playerok.adapter import APIError
from app.services.orders import Orders
from app.workers.sync import Synchronizer
from tests.test_unified_delivery_controls import shop as shop_fixture

shop = shop_fixture


@pytest.mark.parametrize(
    "latency,jitter,limited", [(3, 0, False), (3, 5, False), (40, 5, False), (3, 5, True)]
)
async def test_full_scan_deadline_includes_read_time_and_preserves_retry_after(
    shop, monkeypatch, latency, jitter, limited
):
    r = await shop()
    clock = [100.0]
    original = r.api.call
    order = r.api.order("new-sale")

    async def boundary(method, **kwargs):
        if method == "get_deals":
            clock[0] += latency
            if limited:
                raise APIError("HTTP_429", retry_after=180)
            return r.api.page("deals", [order])
        if method == "get_chats":
            return r.api.page("chats", [])
        return await original(method, **kwargs)

    r.api.call = boundary
    sync = Synchronizer(r.store, r.api, Orders(r.store))
    stop, delays = asyncio.Event(), []

    async def wait(tasks, *, timeout, return_when):
        delays.append(timeout)
        stop.set()
        return set(), set(tasks)

    monkeypatch.setattr("app.workers.sync.time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr("app.workers.sync.random.uniform", lambda *_: jitter)
    monkeypatch.setattr("app.workers.sync.asyncio.wait", wait)
    await sync.run(stop)
    assert len(delays) == 1
    if limited:
        # The run loop wakes at most every60s but must retain the server deadline.
        assert delays[0] == 60
        assert sync.health["orders_retry_after"] == 180
        assert not r.store.rows("SELECT * FROM orders")
    else:
        assert delays[0] == max(1, 30 - jitter - latency)
        assert r.store.rows("SELECT id FROM orders") == [{"id": "new-sale"}]
    assert not r.store.rows("SELECT * FROM outbox") and not r.api.messages
