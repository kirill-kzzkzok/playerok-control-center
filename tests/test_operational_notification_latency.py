"""Real notification dispatcher, isolated SQLite and controlled Telegram boundary."""

import asyncio
from types import SimpleNamespace

from app.database.store import Store, encode, now
from app.services.orders import Orders
from app.workers.notifications import Notifications


async def test_critical_send_precedes_one_hundred_slow_historical_edits(tmp_path):
    store = Store(tmp_path / "latency.sqlite")
    store.claim_first_owner(7)
    store.set_setting("notifications:initialized", True)
    for i in range(100):
        Orders(store).ingest_message("chat", {"id": str(i), "text": "old"})
        store.execute(
            "INSERT INTO notifications(event_key,owner_id,status,telegram_message_id,rendered_payload) VALUES(?,7,'SUCCEEDED',?,'outdated')",
            ("message:" + str(i), i + 1),
        )
    store.execute(
        "INSERT INTO inbox VALUES('critical','enrollment_blocked',?,?,?)",
        (encode({"order_id": "paid"}), now(), now()),
    )
    calls = []
    entered = asyncio.Event()

    async def send(*args, **kwargs):
        calls.append("send")
        return SimpleNamespace(message_id=101)

    async def edit(*args, **kwargs):
        calls.append("edit")
        entered.set()
        await asyncio.Event().wait()

    bot = SimpleNamespace(send_message=send, edit_message_text=edit)
    task = asyncio.create_task(Notifications(store, bot).once())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert calls[0] == "send"
        await asyncio.wait_for(task, 5)
        assert store.rows("SELECT status FROM notifications WHERE event_key='critical'") == [
            {"status": "SUCCEEDED"}
        ]
        assert calls.count("send") == 1
        metrics = store.setting("notifications:timing")
        assert metrics["background_budget_exhausted"]
        assert metrics["background_ms"] < 4500
        assert not store.rows("SELECT * FROM outbox")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        store.close()
