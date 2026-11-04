"""Real Gift/Outbox recovery at API and proxy transport boundaries only."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.gifts.service import GiftService
from app.localvpn import active_proxy
from app.playerok.adapter import APIError
from app.services.orders import Orders
from app.services.product_readiness import ProductReadiness
from tests.test_logical_faults import reopen
from tests.test_stage_a_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


async def prepared(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    Orders(r.store).ingest(r.api.order("waiting-new-order"))
    assert GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender).discover() == 1
    return r


async def test_five_proven_read_failures_recover_after_restart_without_second_operation(shop):
    r = await prepared(shop)
    original = r.api.call
    failures = 0

    async def flaky(method, **kwargs):
        nonlocal failures
        if method in {"get_deal", "get_delivery_deal"} and failures < 5:
            failures += 1
            raise APIError("HTTP_503")
        return await original(method, **kwargs)

    r.api.call = flaky
    ident = r.store.rows("SELECT operation_id FROM gift_jobs")[0]["operation_id"]
    for index in range(5):
        await r.worker.once()
        assert r.store.rows("SELECT status,started_at FROM outbox WHERE id=?", (ident,)) == [
            {"status": "RETRYABLE_FAILED", "started_at": None}
        ]
        assert not r.api.remote.payments
        assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "PENDING"}]
        if index == 2:
            reopen(r)
        r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (ident,))
    await r.worker.once()
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
    assert len(r.api.remote.payments) == 1
    assert len(r.store.rows("SELECT id FROM outbox WHERE kind='gift_send'")) == 1
    assert not await r.worker.once()


@pytest.mark.parametrize("stop", [False, True])
async def test_proxy_outage_does_not_require_manual_reenable_or_override_stop(shop, stop):
    r = await prepared(shop)
    r.api.gift_settings = replace(r.api.gift_settings, telegram_transport="localvpn")
    r.api.gift_sender.settings = r.api.gift_settings

    async def confirm():
        assert proxy.ready
        return proxy.state["generation"]

    proxy = SimpleNamespace(ready=False, state={"generation": 1}, confirm=confirm)
    token = active_proxy.set(proxy)
    try:
        result = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
        assert not result["ready"]
        ident = r.store.rows("SELECT operation_id FROM gift_jobs")[0]["operation_id"]
        await r.worker.once()
        assert r.store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [
            {"status": "RETRYABLE_FAILED"}
        ]
        assert not r.api.remote.payments
        if stop:
            GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender).stop(7)
        proxy.ready = True
        r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (ident,))
        await r.worker.once()
        assert len(r.api.remote.payments) == (0 if stop else 1)
        if stop:
            assert r.store.setting("gifts:stopped")
        else:
            assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
    finally:
        active_proxy.reset(token)


async def test_unknown_payment_never_becomes_an_automatic_preflight_retry(shop):
    r = await prepared(shop)
    r.api.remote.pay_error = TimeoutError()
    await r.worker.once()
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "UNCERTAIN"}]
    assert len(r.api.remote.payments) == 1
    reopen(r)
    for _ in range(5):
        assert not await r.worker.once()
    assert len(r.api.remote.payments) == 1
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "UNCERTAIN"}]


async def test_repeated_restart_before_dispatch_retains_proof_and_one_paid_intent(shop):
    r = await prepared(shop)
    ident = r.store.rows("SELECT operation_id FROM gift_jobs")[0]["operation_id"]
    for _ in range(6):
        action = r.store.claim(operation_id=ident)
        assert action
        r.store.execute("UPDATE outbox SET lease_until=0 WHERE id=?", (ident,))
        r.store.recover()
        row = r.store.rows("SELECT status,started_at FROM outbox WHERE id=?", (ident,))[0]
        assert row == {"status": "RETRYABLE_FAILED", "started_at": None}
        r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (ident,))
    await r.worker.once()
    assert len(r.api.remote.payments) == 1
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]


async def test_long_preflight_outage_is_visible_once_and_clears_after_recovery(shop):
    from app.telegram.home_panel import HomePanel

    r = await prepared(shop)
    original = r.api.call
    broken = True

    async def read_boundary(method, **kwargs):
        if broken and method in {"get_deal", "get_delivery_deal"}:
            raise APIError("HTTP_503")
        return await original(method, **kwargs)

    r.api.call = read_boundary
    ident = r.store.rows("SELECT operation_id FROM gift_jobs")[0]["operation_id"]
    # Controlled age of the local waiting operation, never the paid deal date.
    r.store.execute("UPDATE outbox SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (ident,))
    for _ in range(4):
        await r.worker.once()
        r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (ident,))
    issues = HomePanel(r.store, r.api).paid_issues()
    assert len(issues) == 1 and issues[0]["id"] == "waiting-new-order"
    assert len(r.store.rows("SELECT 1 FROM inbox WHERE kind='delivery_delayed'")) == 1
    broken = False
    await r.worker.once()
    assert not HomePanel(r.store, r.api).paid_issues()
    assert len(r.api.remote.payments) == 1


async def test_prolonged_outage_groups_orders_into_one_owner_notice(shop):
    from app.workers.notifications import Notifications

    r = await prepared(shop)
    Orders(r.store).ingest(r.api.order("zz-second-waiting-order"))
    assert GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender).discover() == 1
    original = r.api.call

    async def offline(method, **kwargs):
        if method in {"get_deal", "get_delivery_deal"}:
            raise APIError("HTTP_503")
        return await original(method, **kwargs)

    r.api.call = offline
    r.store.execute("UPDATE outbox SET created_at='2000-01-01T00:00:00+00:00' WHERE kind='gift_send'")
    for _ in range(4):
        await r.worker.once()
        await r.worker.once()
        r.store.execute("UPDATE outbox SET retry_at=0 WHERE kind='gift_send'")
    assert len(r.store.rows("SELECT 1 FROM inbox WHERE kind='delivery_delayed'")) == 1
    notifications = Notifications(r.store, r.bot)
    for _ in range(2):
        await notifications.once()
    notices = [
        c
        for c in r.session.calls
        if c.__api_method__ == "sendMessage" and "Выдача ожидает восстановления" in c.text
    ]
    assert len(notices) == 1 and "2" in notices[0].text
    assert not r.api.remote.payments
