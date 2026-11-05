"""Catalog extension via production gift services; all MTProto payments are simulated."""

import json

import pytest
from aiogram import Bot

from app.gifts.core import SEED_GIFTS, GiftValidationError, gift_code_from_description
from app.gifts.mtproto import PersonalGiftSender
from app.gifts.service import GiftService
from tests import test_gifts as shared
from tests.test_gift_bundles import BundleTransport, setup
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher

OLD_ID = 5170233102089322756
NEW_ID = 5992502615223568003


@pytest.fixture
def rig(tmp_path):
    yield from shared.rig.__wrapped__(tmp_path)


@pytest.mark.parametrize("codes", ["zombie_bear", "zombie_bear, rose", "zombie_bear, zombie_bear"])
def test_codes_and_ids_are_distinct_and_ordered(codes):
    assert gift_code_from_description("Description\nGIFT: " + codes.upper()) == codes
    assert SEED_GIFTS["bear"][1:] == (OLD_ID, 15)
    assert SEED_GIFTS["zombie_bear"] == ("🧸", NEW_ID, 50)


async def fresh_service(r):
    transport = BundleTransport()
    del transport.catalog["bear"]
    r.api.gift_sender = PersonalGiftSender(r.api.gift_settings, client=transport)
    r.service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    await r.service.refresh()
    return transport


async def test_refresh_missing_old_bear_new_price_and_dispatcher_catalog(rig):
    r = rig
    transport = await fresh_service(r)
    old = r.store.rows("SELECT * FROM gift_catalog WHERE code='bear'")[0]
    assert old["available"] == 0 and old["gift_id"] == str(OLD_ID)
    assert r.service.catalog_bundle("zombie_bear")[0] == {
        "code": "zombie_bear",
        "gift_id": str(NEW_ID),
        "stars": 50,
    }
    with pytest.raises(GiftValidationError):
        r.service.catalog_bundle("bear")
    transport.catalog["zombie_bear"].stars = 60
    await r.service.refresh()
    assert r.service.catalog_bundle("zombie_bear")[0]["stars"] == 60
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    try:
        ui = Interaction(bot, dispatcher(r.store, r.api))
        await ui.click("gft:catalog")
        text = session.calls[-1].text
        assert "🧟 Зомби-медведь · 60 ⭐ · доступен" in text
        assert "🧸 Медведь · 15 ⭐ · недоступен" in text
        assert str(NEW_ID) not in text and str(OLD_ID) not in text
    finally:
        await bot.session.close()
    assert not transport.payments


@pytest.mark.parametrize(
    "codes,total", [("zombie_bear", 50), ("zombie_bear, rose", 75), ("zombie_bear, zombie_bear", 100)]
)
async def test_new_bundles_prepare_and_send_exact_ids_once(rig, codes, total):
    r = rig
    t = await setup(r, codes)
    expected = codes.split(", ")
    steps = r.service.steps("order")
    assert [v["code"] for v in steps] == expected
    assert sum(v["stars"] for v in steps) == total
    assert [int(v["gift_id"]) for v in steps] == [SEED_GIFTS[c][1] for c in expected]
    assert json.loads(r.service.listing("item")["bundle"]) == [
        {"code": c, "gift_id": str(SEED_GIFTS[c][1]), "stars": SEED_GIFTS[c][2]} for c in expected
    ]
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    try:
        ui = Interaction(bot, dispatcher(r.store, r.api))
        await ui.click("gft:bind:item")
        assert "Зомби-медведь" in session.calls[-1].text and str(total) in session.calls[-1].text
        for _ in expected:
            await r.worker.once()
        assert r.service.job("order")["state"] == "SUCCEEDED"
        assert [v.invoice.gift_id for v in t.payments] == [SEED_GIFTS[c][1] for c in expected]
        await ui.click("gft:job:order")
        assert "Зомби-медведь" in session.calls[-1].text
        assert f"{len(expected)}/{len(expected)}" in session.calls[-1].text
        await r.worker.once()
        assert len(t.payments) == len(expected)
    finally:
        await bot.session.close()


@pytest.mark.parametrize(
    "change,reason,state",
    [
        ("price", "PRICE_CHANGED", "BLOCKED"),
        ("missing", "GIFT_UNAVAILABLE", "PENDING"),
        ("sold_out", "GIFT_UNAVAILABLE", "PENDING"),
    ],
)
async def test_changed_or_unavailable_new_gift_blocks_before_payment(rig, change, reason, state):
    r = rig
    t = await setup(r, "zombie_bear")
    if change == "price":
        t.catalog["zombie_bear"].stars = 51
    elif change == "missing":
        del t.catalog["zombie_bear"]
    else:
        t.catalog["zombie_bear"].sold_out = True
    await r.worker.once()
    assert not t.payments
    assert r.service.job("order")["state"] == state
    assert r.service.job("order")["error_code"] == reason
    assert r.store.rows("SELECT started_at FROM outbox WHERE kind='gift_send'") == [{"started_at": None}]
    assert r.service.steps("order")[0]["stars"] == 50


async def test_refresh_does_not_rewrite_old_binding_jobs_steps_or_unknown_payment(rig):
    r = rig
    t = await setup(r, "bear, bear")
    await r.worker.once()
    t.pay_error = TimeoutError()
    await r.worker.once()
    assert r.service.job("order")["state"] == "UNCERTAIN"
    snapshot = {
        table: r.store.rows("SELECT * FROM " + table)
        for table in ("gift_listings", "gift_jobs", "gift_steps", "outbox")
    }
    del t.catalog["bear"]
    await r.service.refresh()
    assert r.store.rows("SELECT available FROM gift_catalog WHERE code='bear'") == [{"available": 0}]
    for table, rows in snapshot.items():
        assert r.store.rows("SELECT * FROM " + table) == rows
    assert all(int(g["gift_id"]) == OLD_ID and g["stars"] == 15 for g in r.service.steps("order"))
    await r.worker.once()
    assert len(t.payments) == 2


async def test_sold_out_is_unavailable_for_new_binding(rig):
    r = rig
    t = await fresh_service(r)
    t.catalog["zombie_bear"].sold_out = True
    await r.service.refresh()
    with pytest.raises(GiftValidationError):
        r.service.catalog_bundle("zombie_bear")
    assert not t.payments
