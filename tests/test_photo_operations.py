"""Real photo UI → durable Outbox → upload boundary; untouched images survive."""

from copy import deepcopy

import pytest
from aiogram.types import Document

from app.services.attachments import Attachments
from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_unified_delivery_controls import state
from tests.test_universal_delivery_e2e import click

shop = shop_fixture


@pytest.fixture
async def photos(shop, tmp_path, monkeypatch):
    files = Attachments(tmp_path / "uploads")
    monkeypatch.setattr("app.telegram.wizard.Attachments", lambda: files)
    monkeypatch.setattr("app.workers.outbox.Attachments", lambda: files)
    r = await shop()
    r.api.item["attachments"] = [{"id": f"old-{i}"} for i in range(3)]

    def update(id, **changes):
        assert id == "lot"
        r.api.mutations.append(deepcopy(changes))
        kept = [a for a in r.api.item["attachments"] if a["id"] not in changes.get("remove_attachments", [])]
        r.api.item["attachments"] = kept + [
            {"id": f"uploaded-{i}"} for i, _ in enumerate(changes.get("add_attachments", []))
        ]
        if r.api.lost_ack:
            raise TimeoutError("test-only lost ACK")
        return deepcopy(r.api.item)

    r.api.account.update_item = update
    return r


@pytest.mark.parametrize("mode", ["add", "replace", "remove", "all"])
async def test_selected_photo_operation_keeps_other_images_and_requires_confirmation(photos, mode):
    r = photos
    await r.ui.click("editphoto:lot")
    if mode in {"replace", "remove"}:
        await click(r, suffix=":select:1")
    await click(r, suffix=":" + mode)
    if mode != "remove":
        await r.ui.message(
            document=Document(
                file_id="photo", file_unique_id="fixture", file_name="image.png", mime_type="image/png"
            )
        )
    assert not r.api.mutations
    data = await state(r).get_data()
    confirmation = "commit:" + data["confirm_token"]
    await r.ui.click(confirmation)
    await r.worker.once()
    ids = [a["id"] for a in r.api.item["attachments"]]
    expected = {
        "add": ["old-0", "old-1", "old-2", "uploaded-0"],
        "replace": ["old-0", "old-2", "uploaded-0"],
        "remove": ["old-0", "old-2"],
        "all": ["uploaded-0"],
    }
    assert ids == expected[mode]
    assert r.store.rows("SELECT status FROM outbox") == [{"status": "SUCCEEDED"}]
    await r.ui.click(confirmation)
    assert not await r.worker.once()
    assert len(r.api.mutations) == 1


@pytest.mark.parametrize("fault", ["changed", "lost"])
async def test_changed_photo_baseline_blocks_and_unknown_upload_never_repeats(photos, fault):
    r = photos
    await r.ui.click("editphoto:lot")
    await click(r, suffix=":add")
    await r.ui.message(
        document=Document(
            file_id="photo", file_unique_id="fixture", file_name="image.png", mime_type="image/png"
        )
    )
    data = await state(r).get_data()
    await r.ui.click("commit:" + data["confirm_token"])
    r.api.lost_ack = fault == "lost"
    if fault == "changed":
        r.api.item["attachments"].append({"id": "external-photo"})
    await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox") == [
        {"status": "UNCERTAIN" if fault == "lost" else "PERMANENT_FAILED"}
    ]
    assert not await r.worker.once()
    assert len(r.api.mutations) == (1 if fault == "lost" else 0)


@pytest.mark.parametrize("reordered", [False, True])
async def test_replacing_first_image_confirms_composition_without_claiming_main_image(photos, reordered):
    from app.telegram.operation_results import OperationResults

    r = photos
    original = r.api.account.update_item

    def update(**changes):
        original(**changes)
        if reordered:
            r.api.item["attachments"].reverse()
        return {"id": "lot"}

    r.api.account.update_item = update
    await r.ui.click("editphoto:lot")
    await click(r, suffix=":select:0")
    await click(r, suffix=":replace")
    await r.ui.message(
        document=Document(
            file_id="photo", file_unique_id="fixture", file_name="image.png", mime_type="image/png"
        )
    )
    shown = r.session.last_panel
    caption = shown.media.caption if hasattr(shown, "media") else shown.caption
    assert "порядок" in caption.lower()
    confirmation = "commit:" + (await state(r).get_data())["confirm_token"]
    await r.ui.click(confirmation)
    await r.worker.once()
    row = r.store.rows("SELECT id,status FROM outbox")[0]
    assert row["status"] == "SUCCEEDED"
    assert {a["id"] for a in r.api.item["attachments"]} == {"old-1", "old-2", "uploaded-0"}
    assert (r.api.item["attachments"][0]["id"] == "uploaded-0") is reordered
    text, _ = OperationResults(r.store).show(7, row["id"])
    assert "порядок и главное фото" in text
    assert "не подтверждает" in text
    assert not await r.worker.once() and len(r.api.mutations) == 1
