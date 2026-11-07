"""Real owner routes and read-only API boundaries, never production credentials."""

import asyncio
from copy import deepcopy

import pytest

from app.services.product_readiness import ProductReadiness
from app.telegram.product_presentation import summary
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


async def configured(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    return r


@pytest.mark.parametrize("stage", ["item", "telegram"])
async def test_readiness_timeout_records_stage_and_recovers_automatically(shop, caplog, stage):
    caplog.set_level("INFO", logger="pcc.shop")
    r = await configured(shop)
    readiness = ProductReadiness(r.store, r.api)
    original = r.api.call
    balance = r.api.gift_sender.balance
    product = readiness.logical.listing("lot")

    async def failed(method, **kwargs):
        if method == "get_item":
            raise TimeoutError()
        return await original(method, **kwargs)

    async def timeout_balance():
        raise TimeoutError()

    if stage == "item":
        r.api.call = failed
    else:
        r.api.gift_sender.balance = timeout_balance
    r.store.set_setting("readiness:lot", {})
    result = await readiness.check(7, "lot")
    assert "автоматически" in r.store.setting("readiness:lot")["error"]
    assert any(f"Readiness stage={stage}" in record.message for record in caplog.records)
    assert not result["ready"]
    r.api.call, r.api.gift_sender.balance = original, balance
    r.store.set_setting("readiness:lot", {})
    await readiness.check(7, "lot", automatic=True)
    assert r.store.setting("readiness:lot")["error"] is None
    assert readiness.logical.listing("lot")["enabled"] == product["enabled"]
    assert not r.api.remote.payments


async def test_automatic_diagnostic_reuses_fresh_stars_but_payment_reads_again(shop):
    from telethon import functions

    from app.gifts.worker import GiftWorker
    from app.services.orders import Orders

    r = await configured(shop)
    r.store.set_setting("readiness:lot", {})
    r.api.remote.calls.clear()
    await ProductReadiness(r.store, r.api).check(7, "lot", automatic=True)
    assert not any(isinstance(call, functions.payments.GetStarsStatusRequest) for call in r.api.remote.calls)
    Orders(r.store).ingest(r.api.order("fresh-payment"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert any(isinstance(call, functions.payments.GetStarsStatusRequest) for call in r.api.remote.calls)
    assert len(r.api.remote.payments) == 1


async def test_status_reuses_recent_verified_details_without_extra_item_reads(shop):
    r = await configured(shop)
    await r.ui.click("rd:check:lot")
    r.store.set_setting("gifts:limits", {"order": 1, "day": 1})
    original = r.api.call

    async def abbreviated(method, **kwargs):
        value = await original(method, **kwargs)
        if method == "get_my_items":
            value["items"] = [
                {key: item[key] for key in ("id", "name", "price", "status", "user")}
                for item in value["items"]
            ]
        return value

    r.api.call = abbreviated
    r.api.calls.clear()
    await r.ui.message("/status")
    text = r.session.last_panel.text
    assert [p["code"] for p in ProductReadiness(r.store, r.api).snapshot("lot")["problems"]] == [
        "STARS_LIMIT"
    ]
    from tests.test_ux_redesign import buttons

    assert set(buttons(r.session.last_panel)) == {"status:close"}
    assert "описания и параметров" not in text
    assert not r.api.calls
    await r.ui.click("item:lot")
    assert "Лимит Stars" in r.session.last_panel.text
    assert not r.api.remote.payments and not r.api.messages


async def test_opened_card_refreshes_safe_description_for_the_same_status_observation(shop):
    r = await configured(shop)
    await r.ui.click("rd:check:lot")
    r.api.item["description"] += " changed outside bot"
    await r.ui.click("item:lot")
    assert "Изменилось описание" not in r.session.last_panel.text
    original = r.api.call

    async def abbreviated(method, **kwargs):
        value = await original(method, **kwargs)
        if method == "get_my_items":
            for item in value["items"]:
                for key in ("description", "category", "obtaining_type"):
                    item.pop(key, None)
        return value

    r.api.call = abbreviated
    await r.ui.message("/status")
    assert "Изменилось описание" not in r.session.last_panel.text
    assert ProductReadiness(r.store, r.api).snapshot("lot")["ready"]


async def test_readiness_uses_one_item_read_and_debounces_repeated_owner_clicks(shop):
    r = await configured(shop)
    r.store.set_setting("readiness:lot", {})
    r.api.calls.clear()
    for _ in range(4):
        await r.ui.click("rd:check:lot")
    assert [method for method, _ in r.api.calls].count("get_item") == 1
    assert not r.api.remote.payments and not r.api.messages


@pytest.mark.parametrize("change", ["price", "account", "expired"])
async def test_partial_projection_cannot_reuse_conflicting_or_expired_evidence(shop, change):
    r = await configured(shop)
    service = ProductReadiness(r.store, r.api)
    await service.check(7, "lot")
    partial = {key: deepcopy(r.api.item[key]) for key in ("id", "name", "price", "status", "user")}
    if change == "price":
        partial["price"] += 1
    elif change == "account":
        r.api.account.id = "another-account"
    else:
        r.api.readiness_item_times[(r.api.account.id, "lot")] = "2020-01-01T00:00:00Z"
    result = service.snapshot("lot", item=partial)
    assert not result["ready"]
    assert "ITEM_UNCHECKED" in {p["code"] for p in result["problems"]}
    assert not r.api.remote.payments


async def test_automatic_check_refreshes_expired_measurements_without_changing_authority(shop):
    r = await configured(shop)
    r.store.set_setting("gifts:balance", {"stars": 0, "at": "2020-01-01T00:00:00Z"})
    r.store.set_setting("gifts:connection", {})
    r.store.set_setting("readiness:lot", {})
    before = r.store.rows("SELECT id,status FROM outbox")
    flags = r.store.rows("SELECT enabled,control_revision FROM logical_products")
    service = ProductReadiness(r.store, r.api)
    assert await service.refresh_due() == 1
    result = service.snapshot("lot")
    assert result["ready"], result["problems"]
    assert await service.refresh_due() == 0
    assert r.store.rows("SELECT id,status FROM outbox") == before
    assert r.store.rows("SELECT enabled,control_revision FROM logical_products") == flags
    assert not r.api.remote.payments and not r.api.messages
    assert not r.store.rows("SELECT * FROM reservations")


async def test_expired_measurements_are_not_presented_as_multiple_owner_failures(shop):
    r = await configured(shop)
    r.store.set_setting("gifts:balance", {"stars": 100, "at": "2020-01-01T00:00:00Z"})
    r.store.set_setting("gifts:connection", {})
    r.store.execute("UPDATE gift_catalog SET checked_at='2020-01-01T00:00:00Z'")
    result = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
    assert not result["ready"]
    text = summary(result, compact=True)
    assert "Требует внимания:" not in text
    assert "обновляются автоматически" in text


async def test_background_and_owner_checks_share_one_read_and_stop_cleanly(shop):
    r = await configured(shop)
    r.store.set_setting("readiness:lot", {})
    original = r.api.call
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def slow_read(method, **kwargs):
        calls.append(method)
        if method == "get_item":
            entered.set()
            await release.wait()
        return await original(method, **kwargs)

    r.api.call = slow_read
    service = ProductReadiness(r.store, r.api)
    task = asyncio.create_task(service.refresh_due())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await r.ui.click("rd:check:lot")
        assert calls == ["get_item"]
    finally:
        release.set()
        await task
    stop = asyncio.Event()
    stop.set()
    await service.run(stop)
    assert calls == ["get_item"]
    assert not r.api.remote.payments


async def test_cached_ui_evidence_never_authorizes_worker_after_external_edit(shop):
    from app.gifts.worker import GiftWorker
    from app.services.orders import Orders

    r = await configured(shop)
    service = ProductReadiness(r.store, r.api)
    await service.check(7, "lot")
    assert service.snapshot("lot")["ready"]
    r.api.item["attributes"] = {"delivery-critical": "changed externally"}
    Orders(r.store).ingest(r.api.order("changed-after-observation"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert not r.api.remote.payments
    assert not r.store.rows("SELECT 1 FROM gift_jobs WHERE state='SUCCEEDED'")


async def test_supervised_loop_survives_restart_without_reusing_memory_as_authority(shop):
    r = await configured(shop)
    r.store.set_setting("readiness:lot", {})
    service = ProductReadiness(r.store, r.api)
    stop = asyncio.Event()
    original = r.api.call
    reads = []

    async def read_and_stop(method, **kwargs):
        result = await original(method, **kwargs)
        reads.append(method)
        stop.set()
        return result

    r.api.call = read_and_stop
    await service.run(stop)
    health = r.store.setting("runtime:cycle:readiness")
    assert health["state"] == "OK" and health["processed"] == 1
    assert reads == ["get_item"]
    for name in ("readiness_items", "readiness_item_times", "readiness_item_versions", "readiness_locks"):
        delattr(r.api, name)
    restarted = ProductReadiness(r.store, r.api)
    assert not restarted.snapshot("lot")["ready"]
    assert await restarted.refresh_due() == 0  # Durable cooldown also survives restart.
    r.store.set_setting("readiness:lot", {"attempt": "2020-01-01T00:00:00Z"})
    stop.clear()
    await restarted.run(stop)
    assert restarted.snapshot("lot")["ready"]
    assert reads == ["get_item", "get_item"]
    assert not r.api.remote.payments and not r.api.messages
