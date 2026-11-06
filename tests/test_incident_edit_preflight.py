from app.playerok.adapter import APIError
from app.telegram.operation_results import OperationResults
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_dispatcher_edit_waits_on_unstarted_read_then_updates_once(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "delivery")
    await r.ui.click("editname:lot")
    await r.ui.message("Updated " + r.api.item["name"])
    await r.ui.click(next(key for key in buttons(r.session.last_panel) if key.startswith("commit:")))
    row = r.store.rows("SELECT * FROM outbox WHERE kind='edit' ORDER BY rowid DESC LIMIT 1")[0]
    original = r.api.call
    before = len(r.api.mutations)

    async def failed(method, **kwargs):
        if method == "get_item":
            raise APIError("HTTP_500")
        return await original(method, **kwargs)

    r.api.call = failed
    await r.worker.once(operation_id=row["id"])
    assert r.store.rows("SELECT status,started_at FROM outbox WHERE id=?", (row["id"],)) == [
        {"status": "RETRYABLE_FAILED", "started_at": None}
    ]
    assert len(r.api.mutations) == before
    r.api.call = original
    r.store.execute("UPDATE outbox SET retry_at=0 WHERE id=?", (row["id"],))
    await r.worker.once(operation_id=row["id"])
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (row["id"],)) == [{"status": "SUCCEEDED"}]
    assert len(r.api.mutations) == before + 1


async def test_old_failed_read_receipt_does_not_claim_playerok_rejected_edit(shop):
    r = await shop()
    await setup_ui(r, "fixed_text", "delivery")
    await r.ui.click("editname:lot")
    await r.ui.message("Updated " + r.api.item["name"])
    await r.ui.click(next(key for key in buttons(r.session.last_panel) if key.startswith("commit:")))
    op = r.store.rows("SELECT id FROM outbox WHERE kind='edit' ORDER BY rowid DESC LIMIT 1")[0]["id"]
    action = r.store.claim(operation_id=op)
    r.store.finish(op, action["lease_token"], "PERMANENT_FAILED", error="HTTP_500")
    text, _ = OperationResults(r.store).show(7, op)
    assert "отклонил запрос" not in text
    assert "не отправлял" in text
