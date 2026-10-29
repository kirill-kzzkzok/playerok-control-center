import json
import os
import time
from pathlib import Path

import pytest

from app.database.store import Store
from app.services.attachments import Attachments
from app.workers.cleanup import UploadCleanup


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "cleanup.sqlite")
    attachments = Attachments(tmp_path / "uploads")
    attachments.directory.mkdir()
    yield store, attachments, UploadCleanup(store, attachments, grace_seconds=3600)
    store.close()


def upload(attachments, name, old=False):
    path = attachments.directory / (name + ".jpg")
    path.write_bytes(b"test")
    if old:
        os.utime(path, (time.time() - 7200, time.time() - 7200))
    return str(path)


def test_cleanup_protects_drafts_uncertain_and_pending_fsm(setup):
    store, attachments, worker = setup
    draft = upload(attachments, "draft", True)
    pending = upload(attachments, "pending", True)
    fsm = upload(attachments, "fsm", True)
    orphan = upload(attachments, "orphan", True)
    recent = upload(attachments, "recent")
    store.draft(1, {"photos": [draft]})
    ident = store.enqueue("edit", "edit", "item", {"add_photos": [pending]})
    action = store.claim()
    store.finish(ident, action["lease_token"], "UNCERTAIN")
    store.execute(
        "INSERT INTO fsm(key,data) VALUES(?,?)",
        ("fsm", json.dumps({"pending": {"payload": {"add_photos": [fsm]}}})),
    )
    result = worker.once()
    assert result["removed"] == 1
    assert not Path(orphan).exists()
    assert all(Path(p).exists() for p in (draft, pending, fsm, recent))


def test_confirmed_upload_can_be_cleaned_immediately(setup):
    store, attachments, worker = setup
    source = upload(attachments, "succeeded")
    ident = store.enqueue("edit", "edit", "item", {"add_photos": [source]})
    action = store.claim()
    store.finish(ident, action["lease_token"], "SUCCEEDED")
    assert worker.once()["removed"] == 1
    assert not Path(source).exists()


def test_cleanup_aborts_on_corrupt_reference_data(setup):
    store, attachments, worker = setup
    source = upload(attachments, "potential-reference", True)
    ident = store.draft(1, {"photos": [source]})
    store.execute("UPDATE drafts SET payload='broken-json' WHERE id=?", (ident,))
    with pytest.raises(ValueError):
        worker.once()
    assert Path(source).exists()


def test_completed_creation_cleans_files_but_keeps_draft_and_binding(setup):
    store, attachments, worker = setup
    source = upload(attachments, "created")
    draft = store.draft(1, {"photos": [source]})
    store.execute("UPDATE drafts SET state='REMOTE_DRAFT' WHERE id=?", (draft,))
    store.execute("INSERT INTO bindings(draft_id,remote_id) VALUES(?,?)", (draft, "remote"))
    ident = store.enqueue("create:" + draft, "create", draft, {"owner": 1})
    action = store.claim()
    store.finish(ident, action["lease_token"], "SUCCEEDED")
    assert worker.once()["removed"] == 1
    assert store.get_draft(draft, 1)["state"] == "REMOTE_DRAFT"
    assert store.rows("SELECT * FROM bindings")[0]["remote_id"] == "remote"
