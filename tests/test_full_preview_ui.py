import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Document

from app.database.store import Store
from app.services.products import Products
from app.telegram.storage import SQLiteStorage
from app.workers.notifications import Notifications
from app.workers.outbox import Executor, OutboxWorker
from tests.test_operations import FakeAPI
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher


class CategoryAPI(FakeAPI):
    async def call(self, method, **kwargs):
        if method == "get_games":
            return {"games": [{"id": "game", "name": "Telegram"}], "page_info": {}}
        if method == "get_game":
            return {
                "id": "game",
                "categories": [
                    {"id": "category", "name": "Звезды"},
                    {"id": "second", "name": "Другая категория"},
                ],
            }
        if method == "get_game_category":
            return {
                "id": kwargs["id"],
                "name": "Звезды" if kwargs["id"] == "category" else "Другая категория",
                "options": [
                    {
                        "field": "amount",
                        "group": "Количество",
                        "type": "SELECTOR",
                        "value": "50",
                        "label": "50 звёзд",
                    }
                ],
            }
        return await super().call(method, **kwargs)

    async def pages(self, method, collection, **kwargs):
        if method == "get_game_category_obtaining_types":
            yield [{"id": "username", "name": "По @username"}, {"id": "gift", "name": "Подарок"}]
        elif method == "get_game_category_data_fields":
            yield [
                {
                    "id": "private",
                    "type": "ITEM_DATA",
                    "input_type": "TEXTAREA",
                    "label": "Данные покупателю",
                    "required": True,
                }
            ]
        else:
            yield []


@pytest.fixture
async def rig(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store, api, session = Store(tmp_path / "preview.sqlite"), CategoryAPI(), TelegramSession()
    store.claim_first_owner(7)
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    yield store, api, session, bot, ui, state
    await bot.session.close()
    store.close()


async def click(ui, state, action):
    await ui.click("w:" + (await state.get_data())["view_token"] + ":" + action)


async def prepare(ui, state, *, long=False):
    await ui.click("create")
    for choice in (0, 0, 1, 0):
        await click(ui, state, "choose:" + str(choice))
    await ui.message("private-seller-field")
    await ui.message("Product name")
    await ui.message("<description>" * (150 if long else 1))
    await ui.message(
        document=Document(
            file_id="photo", file_unique_id="unique", file_name="image.png", mime_type="image/png"
        )
    )
    await click(ui, state, "photos_done")
    await ui.message("1000")
    await click(ui, state, "choose:2")
    await ui.message("first-private-unit\nfirst-private-unit\nsecond-private-unit")
    await click(ui, state, "confirm_done")


async def test_complete_preview_real_wizard_then_create_and_one_time_stock_transfer(rig):
    store, api, session, bot, ui, state = rig
    await prepare(ui, state)
    text = session.calls[-1].text
    for label in ("Telegram", "Звезды", "Подарок", "50 звёзд", "1000", "2 уникальных", "Фотографий: 1"):
        assert label in text
    for method in session.calls:
        assert not any(
            secret in (getattr(method, "text", None) or "")
            for secret in ("private-seller-field", "first-private-unit", "second-private-unit")
        )
    await click(ui, state, "photo_preview:0")
    assert any(call.__api_method__ in {"sendPhoto", "editMessageMedia"} for call in session.calls)
    await click(ui, state, "submit")
    await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
    await Notifications(store, bot).once()
    await ui.click("stock:view:remote1")
    draft = store.rows("SELECT id FROM drafts")[0]["id"]
    await ui.click("stock:transfer:" + draft)
    pending = (await state.get_data())["stock_pending"]
    await ui.click("stock:commit:" + pending["nonce"])
    assert len(store.rows("SELECT * FROM inventory WHERE item_id='remote1'")) == 2
    assert not __import__("json").loads(store.rows("SELECT payload FROM delivery_policies")[0]["payload"])[
        "enabled"
    ]


@pytest.mark.parametrize(
    "target,new_value", [("name", "New product name"), ("description", "New description"), ("price", "749")]
)
async def test_edit_one_preview_field_survives_restart_without_reentering_others(rig, target, new_value):
    store, api, _session, _bot, ui, state = rig
    await prepare(ui, state)
    ident = (await state.get_data())["draft_id"]
    before = store.get_draft(ident, 7)["payload"]
    await click(ui, state, "edit:" + target)
    ui.dp = dispatcher(store, api)
    await ui.message(new_value)
    after = store.get_draft(ident, 7)["payload"]
    assert after["step"] == "preview"
    for key in ("name", "description", "price", "photos", "fields", "delivery_data", "obtaining_type_id"):
        if key != target:
            assert after[key] == before[key]
    assert after[target] == (int(new_value) if target == "price" else new_value)


async def test_preview_change_category_preserves_other_inputs_and_full_long_text(rig):
    store, api, session, _bot, ui, state = rig
    await prepare(ui, state, long=True)
    ident = (await state.get_data())["draft_id"]
    before = store.get_draft(ident, 7)["payload"]
    await click(ui, state, "edit:category")
    await click(ui, state, "choose:1")
    ui.dp = dispatcher(store, api)
    await click(ui, state, "choose:0")
    await click(ui, state, "choose:0")
    await ui.message("new-private-field")
    payload = store.get_draft(ident, 7)["payload"]
    assert payload["step"] == "preview" and payload["category_id"] == "second"
    assert payload["obtaining_type_id"] == "username"
    assert payload["description"] == before["description"] and payload["photos"] == before["photos"]
    pages = [session.calls[-1].text]
    while any(b.text == "Превью →" for row in session.calls[-1].reply_markup.inline_keyboard for b in row):
        action = next(
            b.callback_data
            for row in session.calls[-1].reply_markup.inline_keyboard
            for b in row
            if b.text == "Превью →"
        )
        await ui.click(action)
        pages.append(session.calls[-1].text)
    assert len(pages) > 1
    assert "".join(page.split("\n\n", 1)[1] for page in pages).count("&lt;description&gt;") == 150
    assert all(len(page) < 4096 and "new-private-field" not in page for page in pages)


async def test_change_obtaining_keep_fields_and_return_from_photos(rig):
    store, _api, session, _bot, ui, state = rig
    await prepare(ui, state)
    ident = (await state.get_data())["draft_id"]
    await click(ui, state, "edit:obtaining")
    await click(ui, state, "choose:0")
    await click(ui, state, "field_keep")
    await click(ui, state, "field_keep")
    assert store.get_draft(ident, 7)["payload"]["step"] == "preview"
    assert "По @username" in session.calls[-1].text
    await click(ui, state, "edit:photos")
    await click(ui, state, "photos_done")
    assert store.get_draft(ident, 7)["payload"]["step"] == "preview"


async def test_incomplete_category_edit_cannot_skip_required_selection(rig):
    store, _api, session, _bot, ui, state = rig
    await prepare(ui, state)
    ident = (await state.get_data())["draft_id"]
    await click(ui, state, "edit:game")
    await click(ui, state, "choose:0")
    await click(ui, state, "preview_return")
    assert "Завершите выбор" in session.calls[-1].text
    assert store.get_draft(ident, 7)["payload"]["step"] == "category"


async def test_audit_connected_create_stock_policy_publish_edit_and_discounts(rig):
    from copy import deepcopy

    from app.playerok.adapter import plain

    store, api, session, bot, ui, state = rig
    remote = {}
    original_create, original_read = api.account.create_item, api.call

    def create(**kwargs):
        remote.update(plain(original_create(**kwargs)), raw_price=kwargs["price"])
        return deepcopy(remote)

    def update(**kwargs):
        remote.update({key: value for key, value in kwargs.items() if key != "id"})
        return deepcopy(remote)

    def publish(**kwargs):
        remote["status"] = "APPROVED"
        return deepcopy(remote)

    async def read(method, **kwargs):
        if method == "get_item" and remote:
            return deepcopy(remote)
        return await original_read(method, **kwargs)

    api.account.create_item, api.account.update_item, api.account.publish_item = create, update, publish
    api.call = read
    api.account.automation_enabled = True
    worker = OutboxWorker(store, Executor(store, api, Products(store, api)))
    await prepare(ui, state)
    await click(ui, state, "submit")
    await worker.once()
    await ui.click("stock:view:remote1")
    draft = store.rows("SELECT id FROM drafts")[0]["id"]
    await ui.click("stock:transfer:" + draft)
    ui.dp = dispatcher(store, api)
    await ui.click("stock:commit:" + (await state.get_data())["stock_pending"]["nonce"])
    await ui.click("auto:item:remote1")
    for action in ("toggle:enabled", "toggle:auto_enroll", "save", "arm"):
        await ui.click("ap:" + (await state.get_data())["automation"]["nonce"] + ":" + action)
    assert len(store.rows("SELECT * FROM inventory")) == 2
    await ui.click("publish:remote1")
    token = (await state.get_data())["confirm_token"]
    await ui.click("commit:" + token)
    await ui.click("commit:" + token)
    await worker.once()
    assert remote["status"] == "APPROVED"
    for action, value in (("editname", "Updated product name"), ("discount", "900"), ("discount", "749")):
        await ui.click(action + ":remote1")
        await ui.message(value)
        ui.dp = dispatcher(store, api)
        await ui.click("commit:" + (await state.get_data())["confirm_token"])
        await worker.once()
    await Notifications(store, bot).once()
    assert remote["name"] == "Updated product name" and remote["price"] == 749 and remote["raw_price"] == 1000
    assert store.rows("SELECT COUNT(*) AS n FROM outbox WHERE kind='publish'") == [{"n": 1}]
    assert all(row["status"] == "SUCCEEDED" for row in store.rows("SELECT status FROM outbox"))
    assert "749" in session.calls[-1].text and "1000" in session.calls[-1].text
