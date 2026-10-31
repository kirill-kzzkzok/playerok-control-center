import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot

from app.database.store import Conflict, Store, encode, now
from app.services.notification_preferences import LABELS, NotificationPreferences
from app.services.orders import Orders
from app.telegram.settings_panel import SettingsPanel
from app.workers.notifications import Notifications
from tests.test_telegram_integration import CatalogAPI, Interaction, TelegramSession, dispatcher


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "preferences.sqlite")
    s.claim_first_owner(7)
    s.set_setting("notifications:initialized", True)
    yield s
    s.close()


def event(store, key, kind, payload):
    store.execute("INSERT INTO inbox VALUES(?,?,?,?,?)", (key, kind, encode(payload), now(), now()))


def test_defaults_validation_owner_and_revision_cas(store):
    preferences = NotificationPreferences(store)
    assert preferences.get(7) == (0, dict.fromkeys(LABELS, True))
    preferences.set(7, 0, "messages", False)
    assert not preferences.enabled(7, "message")
    assert preferences.enabled(7, "order")
    assert preferences.enabled(7, "operation_uncertain")
    with pytest.raises(Conflict):
        preferences.set(7, 0, "orders", False)
    with pytest.raises(Conflict):
        preferences.set(8, 0, "orders", False)
    with pytest.raises(ValueError):
        preferences.set(7, 1, "messages", 0)
    with pytest.raises(ValueError):
        preferences.set(7, 1, "operation_uncertain", False)
    assert len(store.rows("SELECT * FROM audit_log WHERE action='NOTIFICATION_PREFERENCE'")) == 1


async def test_disabled_message_batch_is_suppressed_without_starving_orders_or_replay(store):
    preferences = NotificationPreferences(store)
    preferences.set(7, 0, "messages", False)
    for i in range(201):
        Orders(store).ingest_message("chat", {"id": str(i), "text": "private buyer message"})
    Orders(store).ingest(
        {"id": "order", "status": "PAID", "direction": "OUT", "item": {"id": "item", "price": 100}}
    )
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    worker = Notifications(store, bot)
    await worker.once()
    await worker.once()
    assert bot.send_message.await_count == 1
    assert len(store.rows("SELECT * FROM notifications WHERE status='SUPPRESSED'")) == 201
    preferences.set(7, 1, "messages", True)
    await Notifications(store, bot).once()
    assert bot.send_message.await_count == 1
    Orders(store).ingest_message("chat", {"id": "new", "text": "new event"})
    await worker.once()
    assert bot.send_message.await_count == 2


async def test_per_owner_preferences_and_critical_unknown_cannot_be_muted(store):
    store.execute("INSERT INTO administrators(user_id) VALUES(?)", (8,))
    preferences = NotificationPreferences(store)
    preferences.set(7, 0, "messages", False)
    Orders(store).ingest_message("chat", {"id": "one", "text": "hello"})
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    await Notifications(store, bot).once()
    assert [call.args[0] for call in bot.send_message.call_args_list] == [8]
    operation = store.enqueue("unknown", "reply", "chat", {"text": "private"})
    claim = store.claim()
    store.finish(operation, claim["lease_token"], "UNCERTAIN")
    for owner in [7, 8]:
        revision, _ = preferences.get(owner)
        for key in LABELS:
            preferences.set(owner, revision, key, False)
            revision += 1
    await Notifications(store, bot).once()
    assert [call.args[0] for call in bot.send_message.call_args_list][-2:] == [7, 8]


@pytest.mark.parametrize(
    "kind,key,payload",
    [
        ("order_problem", "problems", {"order_id": "order", "item_name": "item"}),
        ("stock_empty", "stock", {"order_id": "order", "item_id": "item"}),
        ("promotion_guard", "promotions", {"reasons": ["limit"]}),
        ("enrollment_blocked", "enrollment", {"order_id": "order"}),
    ],
)
async def test_category_settings_apply_to_urgent_notices_before_baseline(store, kind, key, payload):
    store.set_setting("notifications:initialized", False)
    Orders(store).ingest(
        {"id": "order", "direction": "OUT", "status": "PAID", "has_problem": True, "item": {"id": "item"}}
    )
    NotificationPreferences(store).set(7, 0, key, False)
    event(store, "notice", kind, payload)
    bot = SimpleNamespace(send_message=AsyncMock())
    await Notifications(store, bot).once()
    bot.send_message.assert_not_called()
    assert store.rows("SELECT status FROM notifications WHERE event_key='notice'") == [
        {"status": "SUPPRESSED"}
    ]


async def test_two_notification_workers_do_not_duplicate_suppressed_or_sent_events(store):
    NotificationPreferences(store).set(7, 0, "orders", False)
    Orders(store).ingest_message("chat", {"id": "one", "text": "hello"})
    Orders(store).ingest({"id": "order", "status": "PAID", "direction": "OUT", "item": {"id": "item"}})
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    await asyncio.gather(Notifications(store, bot).once(), Notifications(store, bot).once())
    assert bot.send_message.await_count == 1
    assert len(store.rows("SELECT * FROM notifications")) == 2


async def test_settings_dispatcher_toggle_restart_stale_callback_and_foreign_access(store):
    session, api = TelegramSession(), CatalogAPI()
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    try:
        await ui.click("settings")
        buttons = [
            button.callback_data for row in session.calls[-1].reply_markup.inline_keyboard for button in row
        ]
        assert {"sp:connections", "sp:security", "sp:notifications", "ar:menu", "providers"} <= set(buttons)
        await ui.click("sp:notifications")
        await ui.click("sp:notify:0:messages:0")
        assert not NotificationPreferences(store).enabled(7, "message")
        await dp.storage.close()
        dp = dispatcher(store, api)
        ui = Interaction(bot, dp)
        await ui.click("sp:notifications")
        assert "🔕 Сообщения" in session.calls[-1].reply_markup.inline_keyboard[0][0].text
        await ui.click("sp:notify:0:messages:1")
        assert "изменились" in session.calls[-1].text
        await ui.click("sp:notify:1:messages:1", owner=8)
        assert not NotificationPreferences(store).enabled(7, "message")
        for action in ["sp:connections", "sp:security", "sp:fulfillment"]:
            await ui.click(action)
            assert len(session.calls[-1].text) < 4096
        assert not api.calls
        assert not store.rows("SELECT * FROM outbox")
    finally:
        await dp.storage.close()
        await bot.session.close()


def test_invalid_persisted_preference_is_not_silently_reenabled(store):
    store.set_setting("notifications:7:preferences", {"revision": 1, "enabled": {"messages": "false"}})
    with pytest.raises(ValueError):
        NotificationPreferences(store).enabled(7, "message")
    assert NotificationPreferences(store).enabled(7, "operation_uncertain")
    with pytest.raises(Conflict):
        SettingsPanel(store, CatalogAPI()).handle("sp:notifications", 8)


async def test_corrupted_preference_suppresses_noncritical_but_preserves_unknown_alert(store):
    store.set_setting("notifications:7:preferences", {"revision": 1, "enabled": {"messages": "false"}})
    Orders(store).ingest_message("chat", {"id": "one", "text": "hello"})
    ident = store.enqueue("unknown", "reply", "chat", {"text": "private"})
    action = store.claim()
    store.finish(ident, action["lease_token"], "UNCERTAIN")
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    await Notifications(store, bot).once()
    assert bot.send_message.await_count == 1
    assert "Неопределённый" in bot.send_message.call_args.args[1]
    assert store.rows("SELECT status FROM notifications WHERE event_key='message:one'") == [
        {"status": "SUPPRESSED_INVALID"}
    ]
    assert store.setting("notifications:7:preferences_error") == "INVALID_PREFERENCES"
