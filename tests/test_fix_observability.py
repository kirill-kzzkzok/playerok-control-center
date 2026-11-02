"""Actual Dispatcher/Outbox/notification lifecycle against stateful API boundaries."""

import json

import pytest
from aiogram import Bot

from app.services.orders import Orders
from app.telegram.operation_results import OperationResults
from app.workers.notifications import Notifications
from tests.stateful_telegram import StatefulTelegram
from tests.test_telegram_integration import Interaction
from tests.test_telegram_transport import production_dispatcher
from tests.test_universal_delivery_e2e import simulation as simulation_fixture


@pytest.fixture
async def case(tmp_path):
    async for factory in simulation_fixture.__wrapped__(tmp_path):
        r = await factory()
        await r.bot.session.close()
        r.session = StatefulTelegram()
        r.bot = Bot("123:" + "x" * 35, session=r.session)
        r.ui = Interaction(r.bot, production_dispatcher(r.store, r.api))
        r.store.execute("DELETE FROM settings WHERE key LIKE 'navigation:%'")
        r.store.set_setting("notifications:initialized", True)
        await r.ui.message("/start")
        yield r


async def notification(r, payload):
    Orders(r.store).ingest_message("chat", payload)
    await Notifications(r.store, r.bot).once()
    return r.store.rows(
        "SELECT telegram_message_id FROM notifications WHERE event_key=?", ("message:" + payload["id"],)
    )[0]["telegram_message_id"]


@pytest.mark.parametrize("kind", ["reply", "mark_read"])
async def test_commit_worker_updates_same_panel_and_authorized_result(case, kind):
    r = case
    reads = []
    r.api.account.mark_chat_as_read = lambda chat_id: (
        reads.append(chat_id) or {"id": chat_id, "unread_messages_counter": 0}
    )
    await r.ui.click(("compose:" if kind == "reply" else "readchat:") + "chat")
    if kind == "reply":
        await r.ui.message("Exactly one buyer response")
    if kind == "mark_read":
        assert not r.store.rows("SELECT * FROM outbox")
        assert not reads  # Retired button may open history, never fake read.
        return
    ident = r.store.rows("SELECT id FROM outbox")[0]["id"]
    await r.ui.click("result:show:" + ident)
    panel = r.store.setting("navigation:123:7")["id"]
    await r.worker.once()
    await Notifications(r.store, r.bot).once()
    assert "подтверждено" in r.session.messages[panel]["text"]
    await r.ui.click("result:show:" + ident)
    assert "подтверждено" in r.session.messages[panel]["text"]
    await r.ui.click("commit:obsolete")
    assert not await r.worker.once()
    assert len(r.api.messages) == (1 if kind == "reply" else 0)
    assert len(reads) == (1 if kind == "mark_read" else 0)
    assert not r.store.rows("SELECT 1 FROM operation_receipts WHERE owner_id<>7")


@pytest.mark.parametrize("text", ["{{ITEM_PAID}}", "{{DEAL_CONFIRMED}}", "{{DEAL_HAS_PROBLEM}}"])
async def test_system_event_fallback_is_human_readable_and_human_literal_is_kept(case, text):
    r = case
    ident = await notification(r, {"id": "system", "user": None, "text": text})
    assert "{{" not in r.session.messages[ident]["text"]
    assert "Неизвестный отправитель" not in r.session.messages[ident]["text"]
    human = await notification(r, {"id": "human", "user": {"id": "buyer", "username": "Buyer"}, "text": text})
    assert text in r.session.messages[human]["text"]
    assert len(r.store.rows("SELECT * FROM inbox WHERE kind='message'")) == 2


async def test_edited_deleted_message_updates_known_notification_without_duplicate(case):
    r = case
    payload = {"id": "edit", "user": {"id": "buyer"}, "text": "Original"}
    ident = await notification(r, payload)
    before = sum(m.__api_method__ == "sendMessage" for m in r.session.calls)
    Orders(r.store).ingest_message("chat", {**payload, "deleted_at": "2026-11-01T00:00:00Z"})
    await Notifications(r.store, r.bot).once()
    assert "удалено" in r.session.messages[ident]["text"]
    assert sum(m.__api_method__ == "sendMessage" for m in r.session.calls) == before
    assert len(r.store.rows("SELECT * FROM chat_message_versions")) == 1
    assert json.loads(r.store.rows("SELECT payload FROM inbox WHERE key='message:edit'")[0]["payload"])[
        "deleted_at"
    ]


async def test_critical_system_problem_is_not_lost_in_initial_baseline(case):
    r = case
    r.store.set_setting("notifications:initialized", False)
    r.store.set_setting("sync:health", {"status": "OK"})
    Orders(r.store).ingest_message("chat", {"id": "critical", "user": None, "text": "{{DEAL_HAS_PROBLEM}}"})
    await Notifications(r.store, r.bot).once()
    row = r.store.rows("SELECT * FROM notifications WHERE event_key='message:critical'")[0]
    assert row["status"] == "SUCCEEDED"
    assert "проблема" in r.session.messages[row["telegram_message_id"]]["text"]


async def test_deleted_operation_view_stops_refresh_and_new_panel_recovers(case):
    r = case
    await r.ui.click("compose:chat")
    await r.ui.message("One confirmed buyer response")
    ident = r.store.rows("SELECT id FROM outbox")[0]["id"]
    await r.ui.click("result:show:" + ident)
    old = r.store.setting("navigation:123:7")["id"]
    await r.worker.once()
    r.session.messages[old]["exists"] = False
    await OperationResults(r.store).refresh_views(r.bot)
    count = len(r.session.calls)
    await OperationResults(r.store).refresh_views(r.bot)
    assert len(r.session.calls) == count
    assert r.store.rows("SELECT status FROM outbox") == [{"status": "SUCCEEDED"}]
    await r.ui.message("/start")
    await r.ui.click("result:show:" + ident)
    await OperationResults(r.store).refresh_views(r.bot)
    assert "подтверждено" in r.session.last_panel.text
    assert len(r.api.messages) == 1
