"""Caught worker-loop failures remain visible independently from task supervision."""

import asyncio
import json
import re

import pytest

from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.notifications import Notifications
from app.workers.supervisor import worker_cycle
from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_caught_chain_failures_are_counted_coalesced_and_recover_without_delivery(shop):
    r = await shop()
    await setup_ui(r, "unique_stock")
    Orders(r.store).ingest(r.api.order("cycle-error"))
    await EnrollmentWorker(r.store, r.api).once()
    original = r.store.rows("SELECT policy FROM delivery_chains")[0]["policy"]
    r.store.execute("UPDATE delivery_chains SET policy=?", ("{broken-secret-policy",))
    stop = asyncio.Event()
    task = asyncio.create_task(ChainWorker(r.store, r.api).run(stop))
    try:
        for _ in range(700):
            if r.store.setting("runtime:cycle:chains", {}).get("consecutive_failures", 0) >= 3:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Caught worker failures are not durably counted")
        health = r.store.setting("runtime:cycle:chains")
        assert health["last_started"] and not health.get("last_success")
        assert health["retry_at"] and health["error_code"] == "JSONDecodeError"
        assert len(r.store.rows("SELECT * FROM inbox WHERE kind='worker_failed'")) == 1
        assert "broken-secret-policy" not in json.dumps(r.store.setting("runtime:cycle:chains"))
        await r.ui.click("home")
        assert "требует проверки" in r.session.last_panel.text
        await r.ui.click("orders")
        assert "attention" in buttons(r.session.last_panel)
        await r.ui.click("attention")
        assert "diagnostics:workers" in buttons(r.session.last_panel)
        await r.ui.click("diagnostics:workers")
        assert int(re.search(r"Ошибок подряд: (\d+)", r.session.last_panel.text).group(1)) >= 3
    finally:
        stop.set()
        await task
    # Reconstruct a worker after repairing only the isolated corrupt observation.
    r.store.execute("UPDATE delivery_chains SET policy=?", (original,))
    restart_stop = asyncio.Event()
    restarted = asyncio.create_task(ChainWorker(r.store, r.api).run(restart_stop))
    try:
        for _ in range(100):
            if r.store.setting("runtime:cycle:chains", {}).get("last_success"):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Worker recovery was not observed")
        health = r.store.setting("runtime:cycle:chains")
        assert health["consecutive_failures"] == 0 and health["processed"] == 1
        assert len(r.store.rows("SELECT * FROM inbox WHERE kind='worker_failed'")) == 1
        assert not r.api.messages and not r.api.remote.payments
        await Notifications(r.store, r.bot).once()
        assert r.store.rows(
            "SELECT n.status FROM notifications n JOIN inbox i ON i.key=n.event_key WHERE i.kind='worker_failed'"
        ) == [{"status": "RESOLVED"}]
    finally:
        restart_stop.set()
        await restarted


async def test_normal_long_gift_cycle_is_not_a_failed_worker_or_write_authority(shop, monkeypatch):
    r = await shop()
    import time

    at = time.time()
    with worker_cycle(r.store, "gifts", 5, max_seconds=180):
        monkeypatch.setattr("app.telegram.home_panel.time.time", lambda: at + 90)
        await r.ui.click("attention")
        assert "diagnostics:workers" not in buttons(r.session.last_panel)
        assert not r.store.rows("SELECT * FROM inbox WHERE kind='worker_failed'")
        assert not r.api.messages and not r.api.remote.payments
    health = r.store.setting("runtime:cycle:gifts")
    assert health["last_success"] and health["consecutive_failures"] == 0


async def test_relist_verification_gate_does_not_send_background_warning(shop):
    from app.playerok.adapter import APIError

    r = await shop()
    for _ in range(3):
        with pytest.raises(APIError), worker_cycle(r.store, "logical-products", 30):
            raise APIError("CONNECTION_NOT_VERIFIED")
    await Notifications(r.store, r.bot).once()
    assert r.store.rows(
        "SELECT n.status FROM notifications n JOIN inbox i ON i.key=n.event_key WHERE i.kind='worker_failed'"
    ) == [{"status": "SUPPRESSED"}]
    assert r.store.setting("runtime:cycle:logical-products")["consecutive_failures"] == 3


async def test_relist_waits_for_viewer_before_scanning_and_resumes(shop):
    from types import SimpleNamespace

    from app.playerok.adapter import APIError
    from app.workers.logical_products import LogicalProductWorker

    r = await shop()
    await setup_ui(r, "fixed_text", "test-unit")
    worker = LogicalProductWorker(r.store, r.api)
    r.api.connection = SimpleNamespace(reads_allowed=False, state={"status": "UNKNOWN"})
    r.api.calls.clear()
    stop = asyncio.Event()
    with pytest.raises(APIError, match="CONNECTION_NOT_VERIFIED"):
        await worker._scan_with_connection_check(stop)
    assert not r.api.calls
    assert not r.store.setting("logical:scan:seller")
    r.api.connection.reads_allowed = True
    r.api.connection.state = {"status": "CONNECTED"}
    assert await worker._scan_with_connection_check(stop) == 1
    assert r.api.calls


async def test_relist_verification_wait_has_bounded_retry_and_recovers(shop, monkeypatch):
    from types import SimpleNamespace

    from app.workers.logical_products import LogicalProductWorker

    r = await shop()
    await setup_ui(r, "fixed_text", "test-unit")
    operations = r.store.rows("SELECT id,status FROM outbox")
    worker = LogicalProductWorker(r.store, r.api)
    r.api.connection = SimpleNamespace(reads_allowed=False, state={"status": "UNKNOWN"})
    stop = asyncio.Event()
    delays = []
    scans = []
    original = worker.once

    async def scan():
        result = await original()
        scans.append(True)
        stop.set()
        return result

    async def restore_connection(awaitable, timeout):
        awaitable.close()
        delays.append(timeout)
        r.api.connection.reads_allowed = True
        r.api.connection.state = {"status": "CONNECTED"}
        raise TimeoutError

    worker.once = scan
    monkeypatch.setattr("app.workers.logical_products.asyncio.wait_for", restore_connection)
    await worker.run(stop)
    assert delays[0] <= 30
    assert scans == [True]
    assert r.store.setting("runtime:cycle:logical-products")["consecutive_failures"] == 0
    assert r.store.rows("SELECT id,status FROM outbox") == operations
    assert not r.api.remote.payments


async def test_relist_real_failure_after_verification_wait_still_alerts(shop):
    from app.playerok.adapter import APIError

    r = await shop()
    for _ in range(3):
        with pytest.raises(APIError), worker_cycle(r.store, "logical-products", 30):
            raise APIError("CONNECTION_NOT_VERIFIED")
    await Notifications(r.store, r.bot).once()
    with pytest.raises(ValueError), worker_cycle(r.store, "logical-products", 60):
        raise ValueError("invalid catalog state")
    await Notifications(r.store, r.bot).once()
    assert r.store.rows(
        "SELECT n.status FROM notifications n JOIN inbox i ON i.key=n.event_key "
        "WHERE i.kind='worker_failed' AND n.status='SUCCEEDED'"
    ) == [{"status": "SUCCEEDED"}]


async def test_verifier_wait_retries_in_same_cycle_before_scan_progress(shop):
    from types import SimpleNamespace

    from app.workers.logical_products import LogicalProductWorker

    r = await shop()
    await setup_ui(r, "fixed_text", "test-unit")
    lock = asyncio.Lock()
    r.api.connection = SimpleNamespace(
        reads_allowed=False, lock=lock, state={"status": "UNKNOWN", "reason": "CHECK_IN_PROGRESS"}
    )
    worker = LogicalProductWorker(r.store, r.api)
    async with lock:
        task = asyncio.create_task(worker._scan_with_connection_check(asyncio.Event()))
        await asyncio.sleep(0)
        r.api.connection.reads_allowed = True
        r.api.connection.state = {"status": "CONNECTED"}
    assert await asyncio.wait_for(task, 2) == 1
    assert not r.store.rows("SELECT 1 FROM inbox WHERE kind='worker_failed'")
