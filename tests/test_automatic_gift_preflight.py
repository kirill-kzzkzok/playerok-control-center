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
        reopen(r)
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


@pytest.mark.parametrize("daily", [100, None])
async def test_unknown_payment_never_becomes_an_automatic_preflight_retry(shop, daily):
    r = await prepared(shop)
    GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender).set_limits(7, 15, daily)
    r.api.remote.pay_error = TimeoutError()
    await r.worker.once()
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "UNCERTAIN"}]
    assert len(r.api.remote.payments) == 1
    reopen(r)
    Orders(r.store).ingest(r.api.order("independent-after-unknown"))
    assert GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender).discover() == 0
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


@pytest.mark.parametrize("fault", ["catalog", "sold-out", "balance"])
@pytest.mark.parametrize("stop", [False, True])
async def test_resource_shortage_recovers_without_rearm_but_never_overrides_stop(shop, fault, stop):
    r = await prepared(shop)
    service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    before = service.listing("lot")
    gift = r.api.remote.catalog["heart"]
    if fault == "catalog":
        del r.api.remote.catalog["heart"]
    elif fault == "sold-out":
        gift.sold_out = True
    else:
        r.api.remote.stars = 0
    ident = r.store.rows("SELECT operation_id FROM gift_jobs")[0]["operation_id"]
    await r.worker.once(operation_id=ident)
    assert r.store.rows("SELECT status,started_at FROM outbox WHERE id=?", (ident,)) == [
        {"status": "RETRYABLE_FAILED", "started_at": None}
    ]
    assert not r.api.remote.payments
    after = service.listing("lot")
    assert after["enabled"] == before["enabled"] and after["revision"] == before["revision"]
    r.api.remote.catalog["heart"] = gift
    gift.sold_out = False
    r.api.remote.stars = 200
    if stop:
        service.stop(7)
    reopen(r)
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (ident,))
    await r.worker.once(operation_id=ident)
    assert len(r.api.remote.payments) == (0 if stop else 1)
    if not stop:
        assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
        assert len(r.store.rows("SELECT id FROM outbox WHERE kind='gift_send'")) == 1


async def test_resource_recovery_cannot_approve_a_higher_gift_price(shop):
    r = await prepared(shop)
    ident = r.store.rows("SELECT operation_id FROM gift_jobs")[0]["operation_id"]
    r.api.remote.stars = 0
    await r.worker.once(operation_id=ident)
    assert not r.api.remote.payments
    r.api.remote.stars = 200
    r.api.remote.catalog["heart"].stars = 16
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (ident,))
    await r.worker.once(operation_id=ident)
    assert not r.api.remote.payments
    assert r.store.rows("SELECT status,error_code FROM outbox WHERE id=?", (ident,)) == [
        {"status": "PERMANENT_FAILED", "error_code": "PRICE_CHANGED"}
    ]
    assert r.store.rows("SELECT enabled FROM gift_listings WHERE item_id=?", ("lot",)) == [{"enabled": 0}]


@pytest.mark.parametrize(
    "block", [None, "stop", "closed", "uncertain", "late", "closed-during-read", "competing"]
)
async def test_old_proven_unstarted_resource_failure_recovers_without_manual_retry(shop, block):
    from app.gifts.worker import GiftWorker
    from tests.test_universal_delivery_e2e import click

    r = await prepared(shop)
    ident = r.store.rows("SELECT operation_id FROM gift_jobs")[0]["operation_id"]
    r.api.remote.stars = 0
    await r.worker.once(operation_id=ident)
    assert not r.api.remote.payments
    # Actual UI/worker-created journal, serialized as the previous release left it.
    r.store.execute(
        "UPDATE outbox SET status='PERMANENT_FAILED',error_code='BALANCE_TOO_LOW' WHERE id=?", (ident,)
    )
    r.store.execute("UPDATE gift_jobs SET state='BLOCKED' WHERE operation_id=?", (ident,))
    r.store.execute("UPDATE gift_steps SET state='BLOCKED' WHERE operation_id=?", (ident,))
    reference = r.store.rows("SELECT reference FROM gift_steps")[0]["reference"]
    if block == "stop":
        await r.ui.click("lp:item:lot")
        await click(r, suffix=":stop")
    elif block == "closed":
        r.api.orders["waiting-new-order"]["status"] = "SENT"
    elif block == "uncertain":
        r.store.execute("UPDATE outbox SET status='UNCERTAIN' WHERE id=?", (ident,))
    elif block == "late":
        r.store.record_late_result(ident, "old-token", "UNCERTAIN", error="LOST_CHECK")
    elif block == "competing":
        # Persisted contradictory legacy ledger: do not send a second delivery.
        Orders(r.store).add_stock("lot", ["legacy-reserved-unit"])
        inventory_id = r.store.rows("SELECT id FROM inventory")[0]["id"]
        r.store.execute("UPDATE inventory SET status='RESERVED' WHERE id=?", (inventory_id,))
        r.store.execute("INSERT INTO reservations VALUES(?,?)", ("waiting-new-order", inventory_id))
    r.api.remote.stars = 200
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (ident,))
    reopen(r)
    if block == "closed-during-read":
        original = r.api.call

        async def closed_while_reading(method, **kwargs):
            if method in {"get_deal", "get_delivery_deal"}:
                r.store.execute(
                    "UPDATE outbox SET error_code='MANUALLY_STOPPED_UNCERTAIN' WHERE id=?", (ident,)
                )
            return await original(method, **kwargs)

        r.api.call = closed_while_reading
    processed = await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    assert processed == (0 if block else 1)
    await r.worker.once(operation_id=ident)
    assert len(r.api.remote.payments) == (0 if block else 1)
    assert r.store.rows("SELECT operation_id,reference FROM gift_steps") == [
        {"operation_id": ident, "reference": reference}
    ]
    assert len(r.store.rows("SELECT id FROM outbox WHERE kind='gift_send'")) == 1
    if not block:
        assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]


@pytest.mark.parametrize("fault", ["network", "balance", "catalog"])
async def test_confirmed_first_gift_does_not_force_manual_recovery_of_next_free_check(shop, fault):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart, heart")
    Orders(r.store).ingest(r.api.order("two-confirmed-gifts"))
    service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    assert service.discover() == 1
    await r.worker.once()
    assert len(r.api.remote.payments) == 1
    first = r.store.rows("SELECT * FROM gift_steps WHERE position=0")[0]
    second = r.store.rows("SELECT * FROM gift_steps WHERE position=1")[0]
    gift = r.api.remote.catalog["heart"]
    if fault == "network":
        r.api.remote.resolve_error = TimeoutError()
    elif fault == "balance":
        r.api.remote.stars = 0
    else:
        del r.api.remote.catalog["heart"]
    await r.worker.once(operation_id=second["operation_id"])
    assert r.store.rows("SELECT status,started_at FROM outbox WHERE id=?", (second["operation_id"],)) == [
        {"status": "RETRYABLE_FAILED", "started_at": None}
    ]
    assert len(r.api.remote.payments) == 1
    assert r.store.rows("SELECT * FROM gift_steps WHERE position=0") == [first]
    r.api.remote.resolve_error = None
    r.api.remote.stars = 200
    r.api.remote.catalog["heart"] = gift
    reopen(r)
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (second["operation_id"],))
    await r.worker.once(operation_id=second["operation_id"])
    assert len(r.api.remote.payments) == 2
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
    assert r.store.rows("SELECT * FROM gift_steps WHERE position=0") == [first]


@pytest.mark.parametrize("fault", ["receipt", "late", "unknown-current"])
async def test_package_free_retry_requires_positive_previous_receipt_and_never_repeats_unknown_payment(
    shop, fault
):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart, heart")
    Orders(r.store).ingest(r.api.order("package-proof"))
    service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    service.discover()
    await r.worker.once()
    first = r.store.rows("SELECT * FROM gift_steps WHERE position=0")[0]
    second = r.store.rows("SELECT * FROM gift_steps WHERE position=1")[0]
    if fault == "receipt":
        r.store.execute("UPDATE gift_steps SET receipt='{}' WHERE position=0")
    elif fault == "late":
        r.store.record_late_result(
            first["operation_id"], "other-token", "UNCERTAIN", error="CONFLICTING_LATE_RPC"
        )
    if fault == "unknown-current":
        r.api.remote.pay_error = TimeoutError()
    else:
        r.api.remote.resolve_error = TimeoutError()
    await r.worker.once(operation_id=second["operation_id"])
    expected = "UNCERTAIN" if fault == "unknown-current" else "PERMANENT_FAILED"
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (second["operation_id"],)) == [
        {"status": expected}
    ]
    count = len(r.api.remote.payments)
    assert count == (2 if fault == "unknown-current" else 1)
    r.api.remote.resolve_error = None
    r.api.remote.pay_error = None
    reopen(r)
    for _ in range(3):
        await r.worker.once(operation_id=second["operation_id"])
    assert len(r.api.remote.payments) == count


async def test_old_partial_resource_failure_resumes_only_next_unstarted_gift(shop):
    from app.gifts.worker import GiftWorker

    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart, heart")
    Orders(r.store).ingest(r.api.order("old-partial-package"))
    GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender).discover()
    await r.worker.once()
    first = r.store.rows("SELECT * FROM gift_steps WHERE position=0")[0]
    second = r.store.rows("SELECT * FROM gift_steps WHERE position=1")[0]
    r.api.remote.stars = 0
    await r.worker.once(operation_id=second["operation_id"])
    assert len(r.api.remote.payments) == 1
    r.store.execute(
        "UPDATE outbox SET status='PERMANENT_FAILED',error_code='BALANCE_TOO_LOW',retry_at=0 WHERE id=?",
        (second["operation_id"],),
    )
    r.store.execute("UPDATE gift_jobs SET state='BLOCKED' WHERE operation_id=?", (second["operation_id"],))
    r.store.execute("UPDATE gift_steps SET state='BLOCKED' WHERE operation_id=?", (second["operation_id"],))
    r.api.remote.stars = 200
    reopen(r)
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once(operation_id=second["operation_id"])
    assert len(r.api.remote.payments) == 2
    assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]
    assert r.store.rows("SELECT * FROM gift_steps WHERE position=0") == [first]
    assert r.store.rows("SELECT operation_id,reference FROM gift_steps WHERE position=1") == [
        {"operation_id": second["operation_id"], "reference": second["reference"]}
    ]


@pytest.mark.parametrize("legacy", [False, True])
async def test_daily_exhaustion_waits_for_next_day_without_new_spending_permission(shop, monkeypatch, legacy):
    from datetime import datetime as RealDateTime
    from datetime import timedelta

    import app.database.store as store_module
    import app.gifts.mtproto as sender_module
    import app.gifts.service as gift_module
    import app.services.automation as authorization_module
    from tests import test_gifts as transport_module

    class Clock(RealDateTime):
        offset = timedelta()

        @classmethod
        def now(cls, tz=None):
            value = RealDateTime.now(tz) + cls.offset
            return cls.fromtimestamp(value.timestamp(), tz)

    for module in (store_module, gift_module, sender_module, authorization_module, transport_module):
        monkeypatch.setattr(module, "datetime", Clock)
    r = await prepared(shop)
    service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    service.set_limits(7, 15, 15)
    Orders(r.store).ingest(r.api.order("daily-waiter"))
    assert service.discover() + service.discover() == 1
    first = service.job("waiting-new-order")["operation_id"]
    second = service.job("daily-waiter")["operation_id"]
    await r.worker.once(operation_id=first)
    await r.worker.once(operation_id=second)
    assert len(r.api.remote.payments) == 1
    assert r.store.rows("SELECT status,error_code,started_at FROM outbox WHERE id=?", (second,)) == [
        {"status": "RETRYABLE_FAILED", "error_code": "DAILY_BUDGET_EXHAUSTED", "started_at": None}
    ]
    if legacy:
        # Prior-release daily exhaustion had a shared permanent budget code.
        r.store.execute(
            "UPDATE outbox SET status='PERMANENT_FAILED',error_code='BUDGET_EXCEEDED' WHERE id=?", (second,)
        )
        r.store.execute("UPDATE gift_jobs SET state='BLOCKED' WHERE operation_id=?", (second,))
        r.store.execute("UPDATE gift_steps SET state='BLOCKED' WHERE operation_id=?", (second,))
    Clock.offset = timedelta(days=1)
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (second,))
    if legacy:
        from app.gifts.worker import GiftWorker

        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once(operation_id=second)
    assert len(r.api.remote.payments) == 2 and service.job("daily-waiter")["state"] == "SUCCEEDED"
    Orders(r.store).ingest(r.api.order("third-same-day"))
    service.discover()
    third = service.job("third-same-day")["operation_id"]
    await r.worker.once(operation_id=third)
    assert len(r.api.remote.payments) == 2
    assert r.store.rows("SELECT error_code,started_at FROM outbox WHERE id=?", (third,)) == [
        {"error_code": "DAILY_BUDGET_EXHAUSTED", "started_at": None}
    ]
    assert service.limits("lot") == (15, 15)


async def test_owner_can_remove_daily_cap_in_existing_form_and_process_sequential_sales(shop):
    from tests.test_universal_delivery_e2e import click
    from tests.test_ux_redesign import buttons

    r = await prepared(shop)
    service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    service.set_limits(7, 15, 15)
    r.api.remote.stars = 360
    listing_limits = r.store.setting("gifts:listing_options:lot")

    await r.ui.click("gft:settings")
    await click(r, action="gft:limits")
    await r.ui.message("15 без суточного лимита")
    assert service.limits("lot") == (15, 15)
    await click(
        r,
        action=next(
            button for button in buttons(r.session.last_panel) if button.startswith("gft:save_limits:")
        ),
    )
    assert service.limits("lot") == (15, None)
    assert r.store.setting("gifts:listing_options:lot") == listing_limits

    for index in range(24):
        if index == 12:
            reopen(r)
            service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
        order_id = "waiting-new-order" if index == 0 else f"sequential-{index}"
        if index:
            Orders(r.store).ingest(r.api.order(order_id))
            assert service.discover() + service.discover() == 1
        operation_id = service.job(order_id)["operation_id"]
        await r.worker.once(operation_id=operation_id)
        await r.worker.once(operation_id=operation_id)
        assert service.job(order_id)["state"] == "SUCCEEDED"
        assert len(r.api.remote.payments) == index + 1
    assert r.api.remote.stars == 0
    Orders(r.store).ingest(r.api.order("empty-balance"))
    assert service.discover() + service.discover() == 1
    await r.worker.once(operation_id=service.job("empty-balance")["operation_id"])
    assert len(r.api.remote.payments) == 24
    assert service.job("empty-balance")["error_code"] == "BALANCE_TOO_LOW"

    await r.ui.click("gft:settings")
    await click(r, action="gft:limits")
    await r.ui.message("15 15")
    await click(
        r,
        action=next(
            button for button in buttons(r.session.last_panel) if button.startswith("gft:save_limits:")
        ),
    )
    assert service.limits("lot") == (15, 15)
    r.api.remote.stars = 100
    Orders(r.store).ingest(r.api.order("restored-daily-cap"))
    assert service.discover() + service.discover() == 1
    await r.worker.once(operation_id=service.job("restored-daily-cap")["operation_id"])
    assert len(r.api.remote.payments) == 24
    assert service.job("restored-daily-cap")["error_code"] == "DAILY_BUDGET_EXHAUSTED"

    service.set_limits(7, 14, None)
    Orders(r.store).ingest(r.api.order("over-per-order-cap"))
    assert service.discover() + service.discover() == 1
    await r.worker.once(operation_id=service.job("over-per-order-cap")["operation_id"])
    assert service.job("over-per-order-cap")["error_code"] == "BUDGET_EXCEEDED"
    service.stop(7)
    Orders(r.store).ingest(r.api.order("after-explicit-stop"))
    assert service.discover() == 0
    assert len(r.api.remote.payments) == 24
