from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from PIL import Image

from app.database.store import Conflict, Store
from app.services.attachments import Attachments, LimitedBuffer
from app.services.products import Products, ValidationError
from app.telegram.storage import SQLiteStorage
from app.telegram.wizard import Wizard


def png(color="purple"):
    stream = BytesIO()
    Image.new("RGB", (40, 40), color).save(stream, "PNG")
    return stream.getvalue()


class NoObtainingAPI:
    account = SimpleNamespace(writes_enabled=False)

    async def call(self, method, **kwargs):
        if method == "get_game_category":
            return {"id": kwargs["id"], "options": []}
        if method == "get_game":
            return {"categories": [{"id": "c", "name": "Category"}]}
        if method == "get_games":
            return {"games": [{"id": "g", "name": "Game"}], "page_info": {}}
        raise AssertionError(method)

    async def pages(self, method, collection, **kwargs):
        yield []


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "wizard.sqlite")
    api = NoObtainingAPI()
    wizard = Wizard(store, api, Products(store, api), Attachments(tmp_path / "uploads"))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=1, user_id=1))
    yield store, wizard, state
    store.close()


async def at_step(store, state, step="photos", **extra):
    ident = store.draft(
        1, {"step": step, "photos": [], "fields": {}, "game_id": "g", "category_id": "c", **extra}
    )
    await state.set_state("wizard")
    await state.set_data({"draft_id": ident, "revision": 0})
    return ident


async def click(wizard, state, action, message=None):
    nonce = (await state.get_data())["view_token"]
    return await wizard.callback(1, state, f"w:{nonce}:{action}", message)


def upload(data, *, file_size=None):
    async def download(file_id, destination):
        destination.write(data)
        destination.seek(0)
        return destination

    return SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        text=None,
        photo=[],
        document=SimpleNamespace(file_id="upload", file_size=file_size),
        bot=SimpleNamespace(download=download),
    )


def test_real_bytes_bounded_without_size_metadata():
    stream = LimitedBuffer(5)
    stream.write(b"1234")
    with pytest.raises(ValidationError):
        stream.write(b"56")
    assert stream.getvalue() == b"1234"


async def test_add_preview_replace_and_remove_saved_photos(setup):
    store, wizard, state = setup
    ident = await at_step(store, state)
    await wizard.screen(1, state)
    await wizard.message(upload(png()), state)
    original = store.get_draft(ident, 1)["payload"]["photos"][0]
    assert Path(original).exists()
    message = SimpleNamespace(answer_photo=AsyncMock())
    await click(wizard, state, "photo_preview:0", message)
    assert message.answer_photo.await_count == 1
    await click(wizard, state, "photo_replace:0")
    assert store.get_draft(ident, 1)["payload"]["replace_photo_index"] == 0
    # Replacement survives opening a fresh FSM context.
    state = FSMContext(SQLiteStorage(store), state.key)
    await wizard.message(upload(png("green")), state)
    new = store.get_draft(ident, 1)["payload"]["photos"][0]
    assert new != original and Path(new).exists() and not Path(original).exists()
    await click(wizard, state, "photo_remove:0")
    assert store.get_draft(ident, 1)["payload"]["photos"] == []
    assert not Path(new).exists()


async def test_failed_revision_does_not_destroy_existing_photo(setup):
    store, wizard, state = setup
    ident = await at_step(store, state)
    await wizard.message(upload(png()), state)
    original = store.get_draft(ident, 1)["payload"]["photos"][0]
    await click(wizard, state, "photo_replace:0")
    row = store.get_draft(ident, 1)
    store.update_draft(ident, 1, row["revision"], row["payload"])
    with pytest.raises(Conflict):
        await wizard.message(upload(png("red")), state)
    assert Path(original).exists()
    assert list(wizard.attachments.directory.glob("*.jpg")) == [Path(original)]


async def test_stale_photo_removal_preserves_file(setup):
    store, wizard, state = setup
    ident = await at_step(store, state)
    await wizard.message(upload(png()), state)
    row = store.get_draft(ident, 1)
    original = row["payload"]["photos"][0]
    store.update_draft(ident, 1, row["revision"], row["payload"])
    with pytest.raises(Conflict):
        await click(wizard, state, "photo_remove:0")
    assert Path(original).exists()


async def test_empty_schema_back_reaches_category_instead_of_loop(setup):
    store, wizard, state = setup
    ident = await at_step(store, state, "obtaining")
    await wizard.screen(1, state)
    payload = store.get_draft(ident, 1)["payload"]
    assert payload["step"] == "fields" and payload["obtaining_type_id"] is None
    assert payload["form"][0]["comment"] is True
    await click(wizard, state, "back")
    assert store.get_draft(ident, 1)["payload"]["step"] == "category"


async def test_choice_pagination_and_forged_negative_index(setup):
    store, wizard, state = setup

    async def get_game(method, **kwargs):
        assert method == "get_game"
        return {"categories": [{"id": f"c{i}", "name": f"Category {i}"} for i in range(30)]}

    wizard.api.call = get_game
    await at_step(store, state, "category")
    await wizard.screen(1, state)
    assert len((await state.get_data())["choices"]) == 12
    with pytest.raises(ValidationError):
        await click(wizard, state, "choose:-1")
    await click(wizard, state, "choice_page:2")
    assert (await state.get_data())["choices"][0]["id"] == "c24"
    assert len((await state.get_data())["choices"]) == 6


async def test_path_outside_upload_root_cannot_be_previewed(setup, tmp_path):
    store, wizard, state = setup
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    await at_step(store, state, photos=[str(secret)])
    await wizard.screen(1, state)
    with pytest.raises(ValidationError):
        await click(wizard, state, "photo_preview:0", SimpleNamespace(answer_photo=AsyncMock()))
    assert secret.exists()


async def test_multi_choice_toggle_persisted_and_requires_explicit_done(setup):
    store, wizard, state = setup
    field = {
        "key": "regions",
        "label": "Regions",
        "kind": "multi",
        "required": True,
        "options": [{"value": "eu", "label": "Europe"}, {"value": "us", "label": "USA"}],
    }
    ident = await at_step(store, state, "fields", form=[field], field_index=0)
    await wizard.screen(1, state)
    with pytest.raises(ValidationError):
        await click(wizard, state, "field_done")
    await click(wizard, state, "choose:0")
    row = store.get_draft(ident, 1)["payload"]
    assert row["field_index"] == 0 and row["fields"]["regions"] == ["eu"]
    state = FSMContext(SQLiteStorage(store), state.key)
    await click(wizard, state, "choose:1")
    await click(wizard, state, "choose:0")
    await click(wizard, state, "field_done")
    row = store.get_draft(ident, 1)["payload"]
    assert row["fields"]["regions"] == ["us"] and row["step"] == "name"
