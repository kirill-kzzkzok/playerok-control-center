"""Temporary deal visibility must not discard authorised native completion."""

import time

import pytest

from app.playerok.adapter import APIError
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_playerok_delivery import configure, purchase
from tests.test_playerok_delivery import shop as shop_fixture

shop = shop_fixture


@pytest.mark.parametrize("conflict", ["wrong_deal", "wrong_seller", "problem", "refund", "unauthorized"])
async def test_independent_read_cannot_override_a_real_sale_conflict(shop, conflict):
    r = await shop()
    await configure(r, reply=False, fulfill=True)
    purchase(r)
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    operation = r.store.rows("SELECT id FROM outbox WHERE kind='fulfill'")[0]["id"]
    original = r.api.call
    marks, detailed = [], []
    r.api.account.update_deal = lambda **kwargs: marks.append(kwargs)

    async def read(method, **kwargs):
        if method == "get_deal":
            raise APIError("UNAUTHORIZED_OR_CHALLENGE" if conflict == "unauthorized" else "HTTP_404")
        result = await original(method, **kwargs)
        if method == "get_delivery_deal":
            detailed.append(method)
            if conflict == "wrong_deal":
                result["id"] = "another-order"
            elif conflict == "wrong_seller":
                result["item"]["user"]["id"] = "another-account"
            elif conflict == "problem":
                result["has_problem"] = True
            elif conflict == "refund":
                result["status"] = "ROLLED_BACK"
        return result

    r.api.call = read
    await r.worker.once(operation_id=operation)
    row = r.store.rows("SELECT status,started_at FROM outbox WHERE id=?", (operation,))[0]
    assert row["status"] != "SUCCEEDED" and row["started_at"] is None
    assert not marks and not r.api.remote.payments
    if conflict == "unauthorized":
        assert not detailed


async def test_missing_native_deal_gets_a_prompt_read_retry_before_chain_creation(shop):
    r = await shop()
    await configure(r, reply=False, fulfill=True)
    purchase(r)
    original = r.api.call

    async def missing(method, **kwargs):
        if method in {"get_deal", "get_delivery_deal"}:
            raise APIError("HTTP_404")
        return await original(method, **kwargs)

    r.api.call = missing
    await EnrollmentWorker(r.store, r.api).once()
    row = r.store.rows("SELECT status,due_at FROM auto_enrollment WHERE order_id='order'")[0]
    assert row["status"] == "PENDING"
    assert 0 < row["due_at"] - time.time() <= 12
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind='fulfill'")
    r.api.call = original
    r.store.execute("UPDATE auto_enrollment SET due_at=0")
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    await r.worker.once(kinds=("fulfill",))
    assert r.api.orders["order"]["status"] == "SENT"
    assert not r.api.remote.payments


@pytest.mark.parametrize("failure", ["short", "both", "already_sent"])
async def test_native_completion_recovers_read_visibility_without_repeat(shop, failure):
    r = await shop()
    await configure(r, reply=False, fulfill=True)
    purchase(r)
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    operation = r.store.rows("SELECT id FROM outbox WHERE kind='fulfill'")[0]["id"]
    original = r.api.call
    short_reads = 0
    calls = []

    def mark(**kwargs):
        calls.append(kwargs)
        return r.api.update_deal(**kwargs)

    r.api.account.update_deal = mark
    if failure == "already_sent":
        r.api.orders["order"]["status"] = "SENT"

    async def missing(method, **kwargs):
        nonlocal short_reads
        if method == "get_deal":
            short_reads += 1
            if failure != "already_sent" or short_reads > 1:
                raise APIError("HTTP_404")
        if method == "get_delivery_deal" and failure == "both":
            raise APIError("HTTP_404")
        return await original(method, **kwargs)

    r.api.call = missing
    await r.worker.once(operation_id=operation)
    if failure == "both":
        assert r.store.rows("SELECT status,started_at FROM outbox WHERE id=?", (operation,)) == [
            {"status": "RETRYABLE_FAILED", "started_at": None}
        ]
        assert not calls
        r.api.call = original
        r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (operation,))
        await r.worker.once(operation_id=operation)
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (operation,)) == [{"status": "SUCCEEDED"}]
    assert r.api.orders["order"]["status"] == "SENT"
    assert len(calls) == (0 if failure == "already_sent" else 1)
    await r.worker.once(operation_id=operation)
    assert len(calls) == (0 if failure == "already_sent" else 1)
    assert not r.api.remote.payments and not r.api.messages
