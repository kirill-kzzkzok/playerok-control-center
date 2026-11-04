"""Adversarial navigation through production Dispatcher; isolated external transports."""

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.telegram.storage import SQLiteStorage
from tests.test_telegram_transport import production_dispatcher
from tests.test_universal_delivery_e2e import click
from tests.test_universal_delivery_e2e import simulation as simulation_fixture
from tests.test_ux_redesign import buttons


@pytest.fixture(name="simulation")
async def _simulation(tmp_path):
    async for value in simulation_fixture.__wrapped__(tmp_path):
        yield value


async def open_logical(r):
    for route in ("items", "item:lot", "iv:delivery:lot"):
        await click(r, action=route)


def state(r):
    return FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))


async def test_logical_back_restores_product_section_without_new_messages(simulation):
    r = await simulation()
    await open_logical(r)
    original_id = r.store.setting("navigation:123:7")["id"]
    for _ in range(3):
        back = next(key for key in buttons(r.session.last_panel) if key.startswith("nav:back:"))
        await click(r, action=back)
        assert "iv:delivery:lot" in buttons(r.session.last_panel)
        r.ui.dp = production_dispatcher(r.store, r.api)
        await click(r, action="iv:delivery:lot")
    assert r.store.setting("navigation:123:7")["id"] == original_id
    assert sum(c.__api_method__ == "sendMessage" for c in r.session.calls) == 1
    assert not r.store.rows("SELECT * FROM outbox")
    assert not r.api.mutations and not r.api.messages and not r.api.remote.payments


async def test_cancel_and_restart_invalidates_stale_commit_and_preserves_new_input(simulation):
    r = await simulation()
    await open_logical(r)
    await click(r, action="lp:method:lot")
    await click(r, suffix=":mode:fixed_text")
    await r.ui.message("private-delivery")
    await click(r, suffix=":preview")
    old_commit = next(key for key in buttons(r.session.last_panel) if key.endswith(":commit"))
    await click(r, suffix=":setup")
    await click(r, suffix=":input:after")
    assert await state(r).get_state() == "logical:after"
    r.ui.dp = production_dispatcher(r.store, r.api)
    await r.ui.click(old_commit)
    assert not r.store.rows("SELECT * FROM outbox")
    assert await state(r).get_state() == "logical:after"
    await r.ui.message("Thanks safely")
    assert "сохранено" in r.session.last_panel.text
    assert "private-delivery" not in r.session.last_panel.text
    assert not r.api.mutations


async def test_owner_only_logical_callbacks_and_group_messages_do_not_touch_fsm(simulation):
    r = await simulation()
    await open_logical(r)
    await click(r, action="lp:method:lot")
    await click(r, suffix=":mode:fixed_text")
    current = await state(r).get_data()
    await r.ui.message("private-group-data", group=True)
    await r.ui.click("lp:item:lot", owner=8)
    assert await state(r).get_data() == current
    assert await state(r).get_state() == "logical:text"
    source = Message(
        message_id=r.store.setting("navigation:123:7")["id"],
        date=r.session.calls[0].model_dump().get("date") or 1,
        chat=Chat(id=-100, type="group"),
        from_user=User(id=123, is_bot=True, first_name="Bot"),
        text="Menu",
    )
    query = CallbackQuery(
        id="group-logical",
        from_user=User(id=7, is_bot=False, first_name="Owner"),
        chat_instance="group",
        message=source,
        data="lp:item:lot",
    )
    await r.ui.dp.feed_update(r.bot, Update(update_id=10000, callback_query=query))
    assert await state(r).get_data() == current
    assert not r.store.rows("SELECT * FROM outbox")
    assert not r.api.mutations and not r.api.messages and not r.api.remote.payments


async def test_uncertain_description_ui_allows_only_read_reconcile_not_duplicate_commit(simulation):
    r = await simulation()
    await open_logical(r)
    await click(r, action="lp:method:lot")
    await click(r, suffix=":mode:fixed_text")
    await r.ui.message("private-delivery")
    await click(r, suffix=":preview")
    old_commit = await click(r, suffix=":commit")
    r.api.lost_ack = True
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"
    await click(r, action="lp:item:lot")
    assert "неизвестен" in r.session.last_panel.text
    assert not any(key.endswith(":arm") for key in buttons(r.session.last_panel))
    reconcile = next(key for key in buttons(r.session.last_panel) if key.endswith(":reconcile"))
    r.ui.dp = production_dispatcher(r.store, r.api)
    await r.ui.click(old_commit)
    assert len(r.store.rows("SELECT * FROM outbox")) == 1
    await click(r, action="home")
    await open_logical(r)
    reconcile = next(key for key in buttons(r.session.last_panel) if key.endswith(":reconcile"))
    r.api.account.writes_enabled = False
    await click(r, action=reconcile)
    assert r.store.rows("SELECT status FROM outbox")[0]["status"] == "SUCCEEDED"
    assert len(r.api.mutations) == 1
    assert r.store.rows("SELECT enabled FROM logical_products")[0]["enabled"] == 0
