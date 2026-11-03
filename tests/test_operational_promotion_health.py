"""Per-item API failures must not be hidden by a healthy dry-run schedule."""

import time

from app.playerok.adapter import APIError
from tests.test_promotion_worker import enable
from tests.test_promotion_worker import setup as setup_fixture

setup = setup_fixture


async def test_degraded_health_coalesces_three_failures_and_recovers(setup):
    store, api, settings, worker = setup
    enable(settings, "selected")
    for item in ("bad", "good-one", "good-two"):
        await settings.target(1, item, "include")
    original = api.call.side_effect

    async def failing(method, **kwargs):
        if method == "get_item" and kwargs["id"] == "bad":
            raise APIError("HTTP_429", retry_after="3600")
        return await original(method, **kwargs)

    api.call.side_effect = failing
    assert await worker.once() == 3
    health = store.setting("promotions:health")
    assert health["status"] == "DEGRADED" and health["failed_items"] == 1
    assert health["last_success"]
    assert (
        store.rows("SELECT due_at FROM promotion_schedule WHERE item_id='bad'")[0]["due_at"]
        >= time.time() + 3590
    )
    assert not store.rows("SELECT * FROM inbox WHERE kind='promotion_error'")
    for _ in range(3):
        store.execute("UPDATE promotion_schedule SET due_at=0")
        await worker.once()
    assert len(store.rows("SELECT * FROM inbox WHERE kind='promotion_error'")) == 1
    assert len(store.rows("SELECT * FROM promotion_evaluations WHERE item_id!='bad'")) == 8
    api.call.side_effect = original
    store.execute("UPDATE promotion_schedule SET due_at=0")
    await worker.once()
    health = store.setting("promotions:health")
    assert health["status"] == "DRY_RUN" and health["failed_items"] == 0
    assert not health["incident"]
    assert not store.rows("SELECT * FROM outbox")
    assert not store.rows("SELECT * FROM promotion_expenses")
