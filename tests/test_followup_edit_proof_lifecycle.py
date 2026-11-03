"""Temporary edit evidence is bounded; unresolved evidence survives cleanup."""

from datetime import UTC, datetime, timedelta

import pytest

from app.database.store import Store, encode
from app.services.attachments import Attachments
from app.services.edit_confirmation import EditConfirmation, payload_hash
from app.workers.cleanup import UploadCleanup
from tests.test_operations import FakeAPI


def test_edit_preparation_avoids_unrelated_large_item_objects(tmp_path):
    store, api = Store(tmp_path / "proof.sqlite"), FakeAPI()
    ident = store.enqueue("edit", "edit", "item", {"name": "Updated name"})
    action = store.claim()
    item = {
        "id": "item",
        "name": "Original name",
        "metadata": "x" * 2_000_000,
        "data_fields": [{"id": "f", "value": "secret" * 50000}],
    }
    try:
        proof = EditConfirmation(store, api).prepare(action, item, action["payload"], [])
        assert len(encode(proof)) < 20000
        assert "secret" not in encode(proof)
        assert store.setting(EditConfirmation.key(ident)) == proof
    finally:
        store.close()


@pytest.mark.parametrize("broken", [None, [], "old", {"confirmed": True}])
async def test_incomplete_proof_never_becomes_success_or_crashes(tmp_path, broken):
    store, api = Store(tmp_path / "broken.sqlite"), FakeAPI()
    payload = {"name": "Updated name"}
    ident = store.enqueue("edit", "edit", "item", payload)
    if isinstance(broken, dict):
        broken["payload_hash"] = payload_hash(payload)
    store.set_setting(EditConfirmation.key(ident), broken)
    try:
        proof = await EditConfirmation(store, api).evidence(
            {"id": ident, "target": "item", "payload": payload}
        )
        assert proof["decision"] == "CURRENT_STATE_ONLY"
        assert not api.account.calls
    finally:
        store.close()


async def test_incomplete_confirmed_receipt_missing_remote_id_is_not_success(tmp_path):
    store, api = Store(tmp_path / "incomplete-result.sqlite"), FakeAPI()
    payload = {"price": 100}
    ident = store.enqueue("edit", "edit", "item", payload)
    action = store.claim()
    confirmation = EditConfirmation(store, api)
    item = await api.call("get_item", id="item")
    proof = confirmation.prepare(action, item, payload, [])
    store.set_setting(confirmation.key(ident), {**proof, "confirmed": True, "observed": item, "result": {}})
    try:
        evidence = await confirmation.evidence({**action, "payload": payload})
        assert evidence["decision"] == "CURRENT_STATE_ONLY"
        assert not api.account.calls
    finally:
        store.close()


def test_cleanup_prunes_only_old_successful_edit_proof_and_backup_preserves_unknown(tmp_path):
    store = Store(tmp_path / "cleanup.sqlite")
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    ids = []
    with store.transaction():
        for index in range(1005):
            ident = store.enqueue(str(index), "edit", "item-" + str(index), {"price": 100})
            status = (
                "SUCCEEDED"
                if index < 1000
                else ["UNCERTAIN", "PROCESSING", "PENDING", "PERMANENT_FAILED", "SUCCEEDED"][index - 1000]
            )
            store.execute("UPDATE outbox SET status=? WHERE id=?", (status, ident))
            store.set_setting(
                EditConfirmation.key(ident),
                {
                    "checked_at": old if index != 1004 else datetime.now(UTC).isoformat(),
                    "evidence": "retained",
                },
            )
            ids.append(ident)
    try:
        cleanup = UploadCleanup(store, Attachments(tmp_path / "uploads"))
        for _ in range(3):
            cleanup.once()
        assert len(store.rows("SELECT key FROM settings WHERE key LIKE 'edit:confirmation:%'")) == 5
        assert len(store.rows("SELECT id FROM outbox")) == 1005
        backup = tmp_path / "backup.sqlite"
        store.backup(backup)
        restored = Store(backup)
        try:
            for ident in ids[-5:]:
                assert restored.setting(EditConfirmation.key(ident)) == store.setting(
                    EditConfirmation.key(ident)
                )
        finally:
            restored.close()
    finally:
        store.close()
