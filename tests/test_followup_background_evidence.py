"""Background UI retirement preserves receipts; real operation/transport paths."""

import asyncio
from copy import deepcopy
from pathlib import Path

from app.database.store import Store
from app.services.orders import Orders
from app.services.replies import Replies
from app.telegram.operation_results import OperationResults
from app.workers.notifications import Notifications
from tests.test_fix_observability import case as case_fixture

case = case_fixture


async def test_action_update_skipped_after_owner_navigation_never_claims_rendered(case):
    r = case
    Orders(r.store).ingest_message("chat", {"id": "old-event", "text": "Question"})
    notice = await r.bot.send_message(7, "Question")
    r.store.execute(
        "INSERT INTO notifications(event_key,owner_id,status,telegram_message_id,rendered_payload) VALUES('message:old-event',7,'SUCCEEDED',?,'prior')",
        (notice.message_id,),
    )
    ident = Replies(r.store).enqueue(
        7,
        "answer-once",
        {
            "kind": "reply",
            "target": "chat",
            "payload": {"text": "Answer"},
            "origin_event": "message:old-event",
        },
    )
    await r.worker.once(operation_id=ident)
    await r.ui.click("ops:list:ALL:0", message_id=notice.message_id)
    screen = deepcopy(r.session.messages[notice.message_id])
    receipts = r.store.rows("SELECT * FROM operation_receipts")
    updater = Notifications(r.store, r.bot)
    await asyncio.gather(updater.refresh_actions(), Notifications(r.store, r.bot).refresh_actions())
    row = r.store.rows(
        "SELECT rendered_status,failures FROM notification_actions WHERE operation_id=?", (ident,)
    )[0]
    assert row == {"rendered_status": None, "failures": -1}
    assert r.store.rows("SELECT rendered_payload FROM notifications WHERE event_key='message:old-event'") == [
        {"rendered_payload": "prior"}
    ]
    calls = len(r.session.calls)
    await Notifications(r.store, r.bot).refresh_actions()
    assert len(r.session.calls) == calls
    backup = Path(r.store.path).with_name("retired-ui-backup.sqlite")
    r.store.backup(backup)
    reopened = Store(backup)
    try:
        await Notifications(reopened, r.bot).refresh_actions()
        assert len(r.session.calls) == calls
        assert reopened.rows(
            "SELECT rendered_status,failures FROM notification_actions WHERE operation_id=?", (ident,)
        ) == [{"rendered_status": None, "failures": -1}]
    finally:
        reopened.close()
    assert r.session.messages[notice.message_id] == screen
    assert r.store.rows("SELECT * FROM operation_receipts") == receipts
    assert len(r.api.messages) == 1 and not await r.worker.once()


async def test_closed_result_view_is_retired_without_false_completion_or_edit(case):
    r = case
    ident = Replies(r.store).enqueue(
        7, "closed-result", {"kind": "reply", "target": "chat", "payload": {"text": "Answer"}}
    )
    await r.ui.click("result:show:" + ident)
    panel = r.store.setting("navigation:123:7")
    await r.ui.click("nav:close:" + panel["token"])
    closed = deepcopy(r.session.messages[panel["id"]])
    await r.worker.once(operation_id=ident)
    await OperationResults(r.store).refresh_views(r.bot)
    assert r.session.messages[panel["id"]] == closed
    row = r.store.rows(
        "SELECT view_message_id,rendered_status FROM operation_receipts WHERE operation_id=?", (ident,)
    )[0]
    assert row["view_message_id"] is None and row["rendered_status"] != "SUCCEEDED"
