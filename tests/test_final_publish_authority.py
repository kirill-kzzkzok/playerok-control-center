"""Legacy navigation and queued publications cannot bypass ListingLaunch."""

import pytest

from tests.test_gifts import rig as rig_fixture
from tests.test_listing_launch import launch as launch_fixture
from tests.test_stability_deal_completeness import shop as shop_fixture


@pytest.fixture
async def shop(tmp_path):
    async for create in shop_fixture.__wrapped__(tmp_path):
        yield create


@pytest.fixture
def launch(tmp_path):
    for rig in rig_fixture.__wrapped__(tmp_path):
        yield launch_fixture.__wrapped__(rig)


async def test_old_publish_callback_opens_single_launch_instead_of_generic_confirmation(shop):
    r = await shop()
    r.api.item["status"] = "DRAFT"
    await r.ui.click("publish:lot")
    assert r.store.rows("SELECT 1 FROM drafts WHERE json_extract(payload,'$.launch_target')='lot'")
    assert not r.store.rows("SELECT * FROM outbox")
    assert not r.api.messages and not r.api.remote.payments


async def test_queued_legacy_publication_without_launch_authority_cannot_dispatch(launch):
    r = launch
    op = r.store.enqueue(
        "old-plain", "publish", "item", {"priority_id": "free", "price": 100, "raw_price": 100}, actor=7
    )
    await r.worker.once()
    assert not r.publications
    assert r.api.item["status"] == "DRAFT"
    assert r.store.rows("SELECT status,started_at FROM outbox WHERE id=?", (op,)) == [
        {"status": "PERMANENT_FAILED", "started_at": None}
    ]


async def test_old_publication_confirmation_cannot_be_reused_after_update(shop):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    from app.telegram.storage import SQLiteStorage

    r = await shop()
    r.api.item["status"] = "DRAFT"
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    # Persisted pre-upgrade FSM, not a final operation state.
    await state.set_data(
        {"confirm_token": "old-publish", "pending": {"kind": "publish", "target": "lot", "payload": {}}}
    )
    await r.ui.click("commit:old-publish")
    assert not r.store.rows("SELECT * FROM outbox")
    assert "публикации устарело" in r.session.last_panel.text
