from copy import deepcopy
from dataclasses import asdict
from io import BytesIO

import pytest
from PIL import Image

from app.database.store import Store, now
from app.playerok.adapter import APIError, plain
from app.services.automation import Automation, DeliveryPolicy
from app.services.fulfillment import Fulfillment
from app.services.orders import Orders
from app.services.products import Products, save_image
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.outbox import Executor, OutboxWorker
from tests.test_operations import FakeAPI, queue_manual_publication


@pytest.mark.parametrize(
    "outcome", ["success", "lost_delivery_ack", "bad_create_receipt", "partial_create_receipt"]
)
async def test_mvp_create_photo_publish_future_sale_stock_delivery_and_owner_confirmation(
    tmp_path, monkeypatch, outcome
):
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / "cycle.sqlite")
    store.claim_first_owner(1)
    api = FakeAPI()
    api.account.automation_enabled = True
    api.account.role = "USER"
    remote, order, sends = {}, {}, []
    original_call = api.call
    original_create = api.account.create_item

    def create(**kwargs):
        result = plain(original_create(**kwargs))
        assert len(kwargs["attachments"]) == 1
        assert kwargs["attachments"][0].startswith(b"\xff\xd8")
        remote.update(result, raw_price=result["price"])
        if outcome == "bad_create_receipt":
            # A partial ACK alone is no longer a failure if fresh server reads
            # fully confirm it. Keep the original UNKNOWN safety assertion by
            # providing genuinely incomplete photo evidence on the read too.
            remote["attachments"] = []
        if outcome in {"bad_create_receipt", "partial_create_receipt"}:
            return {"id": result["id"]}
        return result

    def publish(**kwargs):
        remote["status"] = "APPROVED"
        return deepcopy(remote)

    def send(chat_id, text):
        assert chat_id == "chat"
        sends.append(text)
        if outcome == "lost_delivery_ack":
            raise APIError("NETWORK_ERROR", uncertain=True)
        return {"id": "mock-delivery-receipt"}

    def fulfill(deal_id, new_status):
        assert deal_id == "order" and new_status.name == "SENT"
        assert sends == ["mock-unique-stock-value"]
        order["status"] = "SENT"
        return deepcopy(order)

    async def call(method, **kwargs):
        if method == "get_item":
            return deepcopy(remote)
        if method == "get_deal":
            return deepcopy(order)
        if method == "get_chat":
            return {"id": "chat", "is_texting_allowed": True}
        return await original_call(method, **kwargs)

    api.account.create_item, api.account.publish_item = create, publish
    api.account.send_message, api.account.update_deal = send, fulfill
    api.call = call
    products = Products(store, api)
    worker = OutboxWorker(store, Executor(store, api, products))
    buffer = BytesIO()
    Image.new("RGB", (32, 32), "white").save(buffer, "PNG")
    source = save_image(buffer.getvalue(), "data/uploads")
    draft_id = store.draft(
        1,
        {
            "category_id": "c",
            "obtaining_type_id": "o",
            "name": "Mock stock product",
            "description": "Mock cycle description",
            "price": 1000,
            "fields": {"__comment": "Mock fixture only"},
            "photos": [source],
        },
    )
    try:
        await products.queue_create(draft_id, 1, 0)
        await worker.once()
        assert not await worker.once()
        assert api.account.calls.count("createItem") == 1
        if outcome == "bad_create_receipt":
            assert store.rows("SELECT status,error_code FROM outbox") == [
                {"status": "UNCERTAIN", "error_code": "CREATE_NOT_CONFIRMED"}
            ]
            assert not store.rows("SELECT * FROM bindings")
            return
        assert store.rows("SELECT remote_id FROM bindings") == [{"remote_id": "remote1"}]
        await queue_manual_publication(store, api, "remote1")
        await worker.once()
        assert remote["status"] == "APPROVED"
        store.set_setting("orders:last_complete_sync", now())
        await Automation(store, api).save(
            1,
            "remote1",
            0,
            asdict(DeliveryPolicy(enabled=True, mode="stock", auto_enroll=True)),
            future_confirmed=True,
        )
        Orders(store).add_stock("remote1", ["mock-unique-stock-value"])
        order.update(
            id="order",
            status="PAID",
            direction="OUT",
            has_problem=False,
            is_automated=False,
            created_at=now(),
            item={"id": "remote1", "user": {"id": "seller", "role": "USER"}},
            chat={"id": "chat"},
        )
        Orders(store).ingest(order)
        Orders(store).ingest(order)
        assert await EnrollmentWorker(store, api).once() == 1
        assert await EnrollmentWorker(store, api).once() == 0
        assert store.rows("SELECT status FROM inventory") == [{"status": "RESERVED"}]
        assert ChainWorker(store, api).once() == 1
        # Restart after reservation and queue creation; execute existing action only.
        path = store.path
        store.close()
        store = Store(path)
        worker = OutboxWorker(store, Executor(store, api, Products(store, api)))
        await worker.once()
        ChainWorker(store, api).once()
        assert sends == ["mock-unique-stock-value"]
        assert not await worker.once()
        if outcome == "lost_delivery_ack":
            assert store.rows("SELECT status FROM inventory") == [{"status": "QUARANTINED"}]
            assert order["status"] == "PAID"
            assert store.rows("SELECT status FROM delivery_chains") == [{"status": "BLOCKED"}]
            return
        assert store.rows("SELECT status FROM inventory") == [{"status": "DELIVERED"}]
        assert store.rows("SELECT status FROM delivery_chains") == [{"status": "SUCCEEDED"}]
        service = Fulfillment(store, api)
        prepared = await service.prepare(1, "order")
        # Existing flow requires explicit owner attestation; no automatic SENT claim.
        await service.queue(1, "order", prepared["snapshot"], {}, True, "mock-owner-confirmed")
        await worker.once()
        assert order["status"] == "SENT"
        assert not await worker.once()
        assert all(row["status"] == "SUCCEEDED" for row in store.rows("SELECT status FROM outbox"))
    finally:
        store.close()
