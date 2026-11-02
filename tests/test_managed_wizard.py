"""Ordinary description + button gift selection → prepared code → first paid order."""

import pytest
from aiogram.types import Document

from app.playerok.adapter import APIError
from app.services.account_binding import bind_account
from app.services.logical_products import LogicalProducts
from tests.test_final_acceptance_e2e import ORDER, AcceptanceHarness
from tests.test_ux_redesign import buttons


@pytest.fixture
async def shop(tmp_path):
    h = AcceptanceHarness(tmp_path / "wizard.sqlite")
    yield h
    await h.bot.session.close()
    h.store.close()


async def fill(h, mode=3):
    await h.ui.message("/start")
    bind_account(h.store, h.api.account.id)
    await h.sync.once()
    h.store.set_setting("gifts:stopped", False)
    await h.ui.click("create")
    for _ in range(3):
        await h.wizard_action("choose:0")
    await h.ui.message("Передам подарок на указанный username")
    await h.ui.message("Telegram Heart")
    await h.ui.message("Обычное описание без технических меток")
    await h.ui.message(
        document=Document(
            file_id="photo", file_unique_id="fixture", file_name="image.png", mime_type="image/png"
        )
    )
    await h.wizard_action("photos_done")
    await h.ui.message("100")
    await h.wizard_action("choose:" + str(mode))
    if mode == 3:
        await h.wizard_action("gift_add:heart")
        await h.wizard_action("gift_done")
    elif mode in {1, 2}:
        await h.ui.message("private-delivery-one" if mode == 1 else "private-one\nprivate-two")
    assert "SENT" not in h.session.last_panel.text
    await h.wizard_action("confirm_done")
    ident = (await h.state.get_data())["draft_id"]
    assert h.store.get_draft(ident, 7)["payload"]["step"] == "preview"
    return ident


async def test_new_gift_wizard_prepares_code_and_automation_before_first_sale(shop):
    h = shop
    draft = await fill(h)
    assert "gift:" not in h.session.last_panel.text
    await h.wizard_action("submit")
    assert h.store.rows("SELECT state,enabled FROM logical_products") == [
        {"state": "PENDING", "enabled": 0}
    ], h.session.last_panel.text
    assert not h.remote.payments
    h.restart()
    await h.worker.once()
    product = LogicalProducts(h.store, h.api).listing(h.api.item["id"])
    assert product and product["state"] == "VERIFIED" and product["enabled"] == 0
    assert h.api.item["name"].endswith("[" + product["code"] + "]")
    assert "gift:" not in h.api.item["description"]
    await h.ui.click("launch:from:" + draft)
    await h.launch_action("check")
    assert any("Опубликовать и запустить" in label for label in buttons(h.session.last_panel).values())
    await h.launch_action("commit")
    assert LogicalProducts(h.store, h.api).listing(h.api.item["id"])["enabled"] == 1
    await h.worker.once()
    assert len(h.api.published) == 1
    await h.sync.once()
    await h.gifts.once()
    await h.worker.once()
    assert len(h.remote.payments) == 1
    assert h.store.rows("SELECT state FROM gift_jobs WHERE order_id=?", (ORDER,)) == [{"state": "SUCCEEDED"}]
    assert not await h.worker.once()


async def test_managed_creation_price_change_does_not_reserve_or_create(shop):
    h = shop
    await fill(h)
    h.remote.gift.stars = 16
    await h.wizard_action("submit")
    assert not h.store.rows("SELECT * FROM logical_products")
    assert not h.store.rows("SELECT * FROM outbox")
    assert not h.api.created and not h.remote.payments


async def test_creation_readback_timeout_is_reconciled_without_second_create_or_enable(shop):
    h = shop
    draft = await fill(h)
    await h.wizard_action("submit")
    call = h.api.call

    async def unavailable(method, **kwargs):
        if method == "get_item" and h.api.created:
            raise APIError("NETWORK_ERROR")
        return await call(method, **kwargs)

    h.api.call = unavailable
    await h.worker.once()
    assert len(h.api.created) == 1
    row = h.store.rows("SELECT id,status FROM outbox")[0]
    assert row["status"] == "UNCERTAIN"
    h.restart()
    assert not await h.worker.once()
    h.api.call = call
    await h.ui.click("ops:show:" + row["id"])
    assert "ops:confirm:" + row["id"] in buttons(h.session.last_panel)
    await h.ui.click("ops:confirm:" + row["id"])
    await h.ui.message("Проверен уникальный код и серверный черновик")
    action = next(key for key in buttons(h.session.last_panel) if key.startswith("ops:apply:"))
    await h.ui.click(action)
    assert h.store.get_draft(draft, 7)["state"] == "REMOTE_DRAFT"
    assert h.store.rows("SELECT state,enabled FROM logical_products") == [{"state": "VERIFIED", "enabled": 0}]
    assert h.store.rows("SELECT status FROM outbox") == [{"status": "SUCCEEDED"}]
    assert len(h.api.created) == 1 and not h.remote.payments


async def ready(h):
    draft = await fill(h)
    await h.wizard_action("submit")
    await h.worker.once()
    await h.ui.click("launch:from:" + draft)
    await h.launch_action("check")
    return (await h.state.get_data())["launch"]["id"]


async def test_failed_publication_releases_only_its_managed_enable(shop):
    h = shop
    await ready(h)
    await h.launch_action("commit")
    h.api.item["price"] = 101
    await h.worker.once()
    assert not h.api.published
    assert h.store.rows("SELECT enabled FROM logical_products") == [{"enabled": 0}]
    assert not h.store.rows("SELECT 1 FROM gift_listings WHERE enabled=1")


async def test_local_queue_failure_rolls_back_code_configuration_and_projection_together(shop, monkeypatch):
    import sqlite3

    h = shop
    ident = await ready(h)
    before = h.store.rows("SELECT * FROM logical_products")
    enqueue = h.store.enqueue

    def failing(key, kind, *args, **kwargs):
        if kind == "publish":
            raise sqlite3.OperationalError("isolated local fault")
        return enqueue(key, kind, *args, **kwargs)

    monkeypatch.setattr(h.store, "enqueue", failing)
    await h.launch_action("commit")
    assert h.store.rows("SELECT * FROM logical_products") == before
    assert not h.store.rows("SELECT * FROM gift_listings")
    assert h.store.get_draft(ident, 7)["state"] == "DRAFT"
    assert not h.api.published and not h.remote.payments


@pytest.mark.parametrize("mode", [1, 2])
async def test_managed_text_and_stock_are_armed_before_publication_without_extra_configuration(shop, mode):
    from app.workers.delivery_chains import ChainWorker
    from app.workers.enrollment import EnrollmentWorker
    from tests.test_gifts import Marketplace

    h = shop
    draft = await fill(h, mode)
    await h.wizard_action("submit")
    await h.worker.once()
    product = LogicalProducts(h.store, h.api).listing(h.api.item["id"])
    assert product and product["handler"] == ("fixed_text" if mode == 1 else "unique_stock")

    def publish(**kwargs):
        assert LogicalProducts(h.store, h.api).listing(h.api.item["id"])["enabled"] == 1
        h.api.published.append(kwargs)
        h.api.item["status"] = "APPROVED"
        h.api.add_order(ORDER)
        return dict(h.api.item)

    h.api.account.publish_item = publish
    h.api.account.send_message = lambda chat_id, text: Marketplace.send_message(h.api, chat_id, text)
    await h.ui.click("launch:from:" + draft)
    await h.launch_action("check")
    await h.launch_action("commit")
    await h.worker.once()
    assert len(h.api.published) == 1
    await h.sync.once()
    for _ in range(5):
        await EnrollmentWorker(h.store, h.api).once()
        ChainWorker(h.store, h.api).once()
        await h.worker.once()
    steps = h.store.rows(
        "SELECT o.status FROM delivery_chain_steps s JOIN outbox o ON o.id=s.operation_id WHERE s.order_id=? AND s.kind IN ('delivery','delivery_text')",
        (ORDER,),
    )
    assert steps == [{"status": "SUCCEEDED"}]
    assert not h.remote.payments
