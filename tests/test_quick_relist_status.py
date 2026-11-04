from copy import deepcopy

import pytest

from app.services.account_binding import bind_account
from tests.test_final_acceptance_e2e import AcceptanceHarness
from tests.test_manual_premium import manual as manual_fixture
from tests.test_ux_redesign import buttons


@pytest.fixture
async def native_paid(tmp_path):
    async for r in manual_fixture.__wrapped__(tmp_path):
        yield r


@pytest.fixture
async def shop(tmp_path):
    h = AcceptanceHarness(tmp_path / "quick.sqlite")
    await h.ui.message("/start")
    bind_account(h.store, h.api.account.id)
    h.api.item.update(
        status="SOLD",
        name="Telegram Heart",
        description="Send the heart to your username",
        comment="gift delivery",
        attributes={},
        data_fields=[],
    )
    old = deepcopy(h.api.item)
    old.update(id="old", may_be_published=True)
    h.old = old
    original = h.api.call

    async def call(method, **kwargs):
        if method == "get_item" and kwargs["id"] == "old":
            return {**deepcopy(old), "id": "old"}
        return await original(method, **kwargs)

    def publish(**kwargs):
        h.api.published.append(kwargs)
        old.update(status="APPROVED", priority="DEFAULT")
        return deepcopy(old)

    h.api.call = call
    h.api.account.publish_item = publish
    yield h
    await h.bot.session.close()
    h.store.close()


async def choose(r, ident="old"):
    await r.ui.click("relist:item:" + ident)
    action = next(k for k in buttons(r.session.last_panel) if ":quote:" in k)
    await r.ui.click(action)
    return next(k for k in buttons(r.session.last_panel) if k.endswith(":commit"))


async def test_native_sold_republish_without_create_or_duplicate(shop):
    h = shop
    confirmation = await choose(h)
    assert not h.api.published and not h.api.created
    await h.ui.click(confirmation)
    await h.ui.click(confirmation)
    await h.worker.once()
    assert len(h.api.published) == 1 and not h.api.created and not h.remote.payments
    assert h.api.published[0]["item_id"] == "old"
    assert not h.store.rows("SELECT * FROM drafts")
    assert h.store.rows("SELECT status FROM outbox") == [{"status": "SUCCEEDED"}]
    assert not await h.worker.once()


async def test_status_only_verified_active_and_unknown_not_green(shop):
    h = shop
    h.api.item.update(status="APPROVED", priority="DEFAULT")
    await h.ui.message("/status")
    assert "АКТИВНЫЕ ТОВАРЫ" in h.session.last_panel.text
    assert "Telegram Heart" in h.session.last_panel.text
    assert "не настроена" in h.session.last_panel.text.lower()
    assert not h.store.rows("SELECT * FROM outbox")
    before = len([c for c in h.session.calls if c.__api_method__ == "sendMessage"])
    await h.ui.click("status:view:0")
    assert len([c for c in h.session.calls if c.__api_method__ == "sendMessage"]) == before
    h.api.item["status"] = "SOLD"
    await h.ui.click("status:view:0")
    assert "Telegram Heart" not in h.session.last_panel.text


@pytest.mark.parametrize("fault", [None, "lost", "price", "stop"])
async def test_native_premium_uses_sold_id_and_existing_payment_safety(native_paid, fault):
    r = native_paid
    bind_account(r.store, r.api.account.id)
    r.api.items["item"].update(status="SOLD", may_be_published=True)
    confirmation = await choose(r, "item")
    assert "10" in r.session.last_panel.text
    assert not r.api.charges
    await r.ui.click(confirmation)
    if fault == "lost":
        r.api.timeout = True
    elif fault == "price":
        r.api.quotes[0]["price"] = 11
    elif fault == "stop":
        from app.services.paid_promotions import PaidPromotions

        PaidPromotions(r.store, r.api).settings.stop(7)
    await r.worker.once()
    await r.ui.click(confirmation)
    assert not await r.worker.once()
    assert len(r.api.charges) == (0 if fault in {"price", "stop"} else 1)
    if r.api.charges:
        assert r.api.charges[0]["item_id"] == "item"
    assert r.store.rows("SELECT status FROM outbox") == [
        {"status": "SUCCEEDED" if fault is None else "UNCERTAIN" if fault == "lost" else "PERMANENT_FAILED"}
    ]


async def test_native_keeps_gift_binding_and_history_on_same_item(shop):
    h = shop
    h.old.update(status="APPROVED", description="Send the heart to your username\ngift: heart")
    await h.service.bind(7, "old", "heart")
    before = h.service.listing("old")
    h.old["status"] = "SOLD"
    await h.ui.click(await choose(h))
    await h.worker.once()
    assert h.service.listing("old") == before
    assert not h.service.listing("new")
    assert not h.api.created and not h.remote.payments


async def test_status_blocked_stock_is_not_green_and_owner_only(shop):
    from dataclasses import asdict

    from app.services.automation import Automation, DeliveryPolicy

    h = shop
    h.api.item.update(status="APPROVED", priority="DEFAULT")
    await Automation(h.store, h.api).save(
        7,
        h.api.item["id"],
        0,
        asdict(DeliveryPolicy(enabled=True, mode="stock", auto_enroll=True)),
        future_confirmed=True,
    )
    await h.ui.message("/status")
    assert "Автовыдача ожидает устранения проблемы" in h.session.last_panel.text
    assert "item:" + h.api.item["id"] in buttons(h.session.last_panel)
    assert "🟢 готово" not in h.session.last_panel.text
    count = len(h.session.calls)
    await h.ui.message("/status", owner=99)
    assert len(h.session.calls) == count
    await h.ui.message("/status", group=True)
    assert len(h.session.calls) == count


async def test_status_pages_only_fetch_requested_page(shop):
    h = shop
    original = h.api.call
    seen = []

    async def call(method, **kwargs):
        if method == "get_my_items":
            seen.append(kwargs.get("after_cursor"))
            return {
                "items": [{"id": "page-2" if kwargs.get("after_cursor") else "page-1"}],
                "page_info": {
                    "has_next_page": not kwargs.get("after_cursor"),
                    "end_cursor": "next" if not kwargs.get("after_cursor") else None,
                },
            }
        if method == "get_item" and kwargs["id"].startswith("page-"):
            return {**deepcopy(h.api.item), "id": kwargs["id"], "name": kwargs["id"], "status": "APPROVED"}
        return await original(method, **kwargs)

    h.api.call = call
    await h.ui.message("/status")
    assert "status:view:1" in buttons(h.session.last_panel)
    await h.ui.click("status:view:1")
    assert "page-2" in h.session.last_panel.text and "page-1" not in h.session.last_panel.text
    assert seen == [None, "next"]
