"""Controlled server timestamp precision at the real first-sale boundary."""

from datetime import UTC, datetime, timedelta

import pytest

from app.database.store import Conflict
from app.services.orders import Orders
from app.workers.enrollment import EnrollmentWorker
from tests.test_final_acceptance_e2e import ORDER, AcceptanceHarness


async def test_newly_created_first_sale_with_server_second_precision_is_not_lost(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    h = AcceptanceHarness(tmp_path / "first-sale.sqlite")
    try:
        h.api.item["created_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
        await h.create_and_prepare()
        await h.launch_action("commit")
        original = h.api.add_order

        def server_second_precision(ident, username):
            order = original(ident, username)
            # Controlled mock server: publication and immediate sale happen in
            # this second, but order.created_at has second-only precision.
            armed = h.api.delivery_at_publish["armed_at"]
            order["created_at"] = datetime.fromisoformat(armed).replace(microsecond=0).isoformat()
            return order

        h.api.add_order = server_second_precision
        assert await h.worker.once()  # actual publication executor, no live API
        assert h.api.item["status"] == "APPROVED"
        assert not h.remote.payments
        await h.sync.once()
        assert h.store.rows("SELECT status FROM orders WHERE id=?", (ORDER,))[0]["status"] == "PAID"
        await h.gifts.once()  # actual discovery decision, not direct job insert
        jobs = h.store.rows("SELECT * FROM gift_jobs WHERE order_id=?", (ORDER,))
        assert len(jobs) == 1, "the first paid sale was skipped because its timestamp lost microseconds"
        await h.worker.once()
        assert len(h.remote.payments) == 1
        assert (
            h.store.rows("SELECT state FROM gift_jobs WHERE order_id=?", (ORDER,))[0]["state"] == "SUCCEEDED"
        )
    finally:
        await h.close()


@pytest.mark.parametrize("mode", ["text", "stock"])
async def test_created_first_sale_uses_server_clock_for_text_or_stock(tmp_path, monkeypatch, mode):
    monkeypatch.chdir(tmp_path)
    h = AcceptanceHarness(tmp_path / (mode + "-first-sale.sqlite"))
    try:
        h.api.item["created_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
        await h.create_and_prepare()
        await h.launch_action("mode:" + mode)
        await h.launch_action("edit:data")
        await h.ui.message("secret-unique-unit" if mode == "stock" else "secret-fixed-text")
        await h.launch_action("check")
        await h.launch_action("commit")
        original = h.api.add_order

        # publish fake asserts a gift binding, so use the same owned server mutation
        # with a policy assertion for text/stock instead of weakening its gift guard.
        def publish(**kwargs):
            policy = h.store.rows("SELECT * FROM delivery_policies WHERE item_id='item'")[0]
            assert policy["updated_at"] == h.api.item["created_at"].replace("+00:00", ".000000+00:00")
            h.api.published.append(kwargs)
            h.api.item["status"] = "APPROVED"
            order = original(ORDER, "@kzzkzok")
            order["created_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
            return dict(h.api.item)

        h.api.account.publish_item = publish
        await h.worker.once()
        await h.sync.once()
        assert not h.store.rows("SELECT * FROM gift_jobs")
        await EnrollmentWorker(h.store, h.api).once()
        chain = h.store.rows("SELECT * FROM delivery_chains WHERE order_id=?", (ORDER,))
        assert len(chain) == 1 and chain[0]["status"] == "ACTIVE"
        if mode == "stock":
            assert h.store.rows("SELECT * FROM reservations WHERE order_id=?", (ORDER,))
        assert not h.remote.payments
    finally:
        await h.close()


@pytest.mark.parametrize(
    "change", ["server_created_at", "previous_publish", "known_sale", "missing_receipt_date"]
)
async def test_creation_provenance_change_revokes_old_preview(tmp_path, monkeypatch, change):
    monkeypatch.chdir(tmp_path)
    h = AcceptanceHarness(tmp_path / (change + ".sqlite"))
    try:
        h.api.item["created_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
        await h.create_and_prepare()
        draft = h.launch.load(7, h.launch_id)
        if change == "server_created_at":
            h.api.item["created_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        elif change == "previous_publish":
            op = h.store.enqueue("old-publish", "publish", "item", {}, actor=7)
            action = h.store.claim(operation_id=op)
            h.store.finish(op, action["lease_token"], "PERMANENT_FAILED", error="VALIDATION_FAILED")
        elif change == "known_sale":
            Orders(h.store).ingest(h.api.add_order("older-sale", "@kzzkzok"))
        else:
            h.store.execute("UPDATE outbox SET result=json_remove(result,'$.created_at') WHERE kind='create'")
        with pytest.raises(Conflict):
            await h.launch.queue(7, h.launch_id, draft["revision"])
        assert not h.api.published and not h.remote.payments
        assert not h.service.listing("item")
    finally:
        await h.close()


async def test_known_sale_appearing_during_prepare_prevents_atomic_commit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    h = AcceptanceHarness(tmp_path / "prepare-sale.sqlite")
    try:
        h.api.item["created_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
        await h.create_and_prepare()
        draft = h.launch.load(7, h.launch_id)
        original = h.api.call

        async def call(method, **kwargs):
            if method == "get_item_priority_statuses":
                Orders(h.store).ingest(h.api.add_order("concurrent-sale", "@kzzkzok"))
            return await original(method, **kwargs)

        h.api.call = call
        with pytest.raises(Conflict):
            await h.launch.queue(7, h.launch_id, draft["revision"])
        assert not h.service.listing("item") and not h.api.published
        assert h.store.get_draft(h.launch_id, 7)["state"] == "DRAFT"
    finally:
        await h.close()
