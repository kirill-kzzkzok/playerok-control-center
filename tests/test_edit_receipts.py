from copy import deepcopy

import pytest

from app.database.store import Store
from app.services.item_receipts import edit_receipt_matches
from app.workers.outbox import Executor, OutboxWorker
from tests.test_operations import FakeAPI


@pytest.mark.parametrize(
    "changes,result",
    [
        ({"name": "New item name"}, {"id": "item"}),
        ({"description": "New description"}, {"id": "item", "description": "Old description"}),
        ({"price": 100}, {"id": "item", "price": None}),
        ({"price": 100}, {"id": "item", "price": "100.001"}),
        ({"price": 100}, {"id": "item", "price": "NaN"}),
        ({"price": 100}, {"id": "item", "price": True}),
        ({"name": "New item name"}, None),
        ({"name": "New item name"}, {"id": "other", "name": "New item name"}),
        (
            {"name": "New item name", "description": "New description"},
            {"id": "item", "name": "New item name"},
        ),
    ],
)
async def test_mismatched_text_price_or_incomplete_edit_receipt_is_unknown_once(tmp_path, changes, result):
    store = Store(tmp_path / "edit.sqlite")
    api = FakeAPI()
    original_read = api.call

    async def read(method, **kwargs):
        value = await original_read(method, **kwargs)
        if method == "get_item" and "price" in changes:
            # The readback must actually disagree. The shared fixture's price
            # 100 was already equal to the requested value before any write.
            value["price"] = 99
        return value

    api.call = read
    calls = []

    def edit(**kwargs):
        calls.append(kwargs)
        return deepcopy(result)

    api.account.update_item = edit
    store.enqueue("edit", "edit", "item", changes)
    worker = OutboxWorker(store, Executor(store, api, None))
    try:
        await worker.once()
        assert not await worker.once()
        assert len(calls) == 1
        assert store.rows("SELECT status,error_code FROM outbox") == [
            {"status": "UNCERTAIN", "error_code": "EDIT_NOT_CONFIRMED"}
        ]
        assert store.rows("SELECT kind FROM inbox") == [{"kind": "operation_uncertain"}]
    finally:
        store.close()


@pytest.mark.parametrize(
    "images",
    [
        None,
        [],
        [{"id": "old"}],
        [{"id": "new"}, {"id": "new"}],
        [{"id": "one"}],
        [None, {"id": "two"}],
        [{"id": ""}, {"id": "two"}],
        [{"id": 123}, {"id": "two"}],
    ],
)
def test_photo_receipt_rejects_old_duplicate_missing_or_partial_ids(images):
    assert not edit_receipt_matches(
        "item",
        {"add_attachments": [b"one", b"two"], "remove_attachments": ["old"]},
        {"id": "item", "attachments": images},
    )


def test_positive_receipt_requires_all_requested_fields_and_new_image_set():
    changes = {
        "name": "New item name",
        "description": "New description",
        "price": 100,
        "add_attachments": [b"one", b"two"],
        "remove_attachments": ["old"],
    }
    result = {
        "id": "item",
        "name": "New item name",
        "description": "New description",
        "price": "100.00",
        "attachments": [{"id": "two"}, {"id": "one"}],
    }
    assert edit_receipt_matches("item", changes, result)
    assert not edit_receipt_matches(
        "item", changes, {**result, "attachments": [{"id": "old"}, {"id": "one"}]}
    )


async def test_failed_photo_receipt_protects_local_file_from_upload_cleanup(tmp_path, monkeypatch):
    from app.services.attachments import Attachments
    from app.workers.cleanup import UploadCleanup

    attachments = Attachments(tmp_path / "uploads")
    attachments.directory.mkdir()
    path = attachments.directory / "photo.jpg"
    path.write_bytes(b"fixture")
    store = Store(tmp_path / "photo.sqlite")
    api = FakeAPI()
    api.account.update_item = lambda **kwargs: {"id": "item", "attachments": []}
    monkeypatch.setattr("app.workers.outbox.Attachments", lambda: attachments)
    store.enqueue("photo", "edit", "item", {"add_photos": [str(path)], "replace_attachment_ids": []})
    worker = OutboxWorker(store, Executor(store, api, None))
    try:
        await worker.once()
        assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
        UploadCleanup(store, attachments, grace_seconds=0).once()
        assert path.exists()
        assert not await worker.once()
    finally:
        store.close()
