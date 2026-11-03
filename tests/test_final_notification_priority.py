"""Bounded priority admits critical warnings without starving ordinary events."""

from aiogram import Bot

from app.database.store import Store, now
from app.services.orders import Orders
from app.workers.notifications import Notifications
from tests.test_telegram_integration import TelegramSession


async def test_uncertain_warning_passes_old_history_but_ordinary_events_keep_a_quota(tmp_path):
    store = Store(tmp_path / "priority.sqlite")
    store.claim_first_owner(7)
    store.set_setting("notifications:initialized", True)
    store.set_setting("playerok:account_id", "seller")
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    for i in range(220):
        Orders(store).ingest_message("chat", {"id": f"old-{i}", "text": "history", "user": {"id": "seller"}})
    operations = []
    for i in range(105):
        ident = store.enqueue("unknown:" + str(i), "reply", "chat", {"text": "test-only"})
        action = store.claim(operation_id=ident)
        store.finish(ident, action["lease_token"], "UNCERTAIN", error="NETWORK_ERROR")
        operations.append(ident)
    # A low-priority unknown event still receives its bounded processing quota.
    store.execute("INSERT INTO inbox VALUES('secondary','secondary','{}',?,?)", (now(), now()))
    try:
        await Notifications(store, bot).once()
        assert (
            len(
                store.rows(
                    "SELECT 1 FROM notifications n JOIN inbox i ON i.key=n.event_key WHERE i.kind='operation_uncertain' AND n.status='SUCCEEDED'"
                )
            )
            == 105
        )
        assert store.rows("SELECT status FROM notifications WHERE event_key='secondary'") == [
            {"status": "SKIPPED"}
        ]
        assert len(store.rows("SELECT * FROM notifications WHERE event_key LIKE 'message:old-%'")) > 0
        assert any("Неопределённый результат" in (getattr(call, "text", "") or "") for call in session.calls)
        assert all(row["status"] == "UNCERTAIN" for row in store.rows("SELECT status FROM outbox"))
    finally:
        await bot.session.close()
        store.close()
