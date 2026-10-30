from dataclasses import asdict
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Store
from app.services.automation import DeliveryPolicy
from app.services.inventory import Inventory
from app.services.orders import Orders
from app.telegram.storage import SQLiteStorage
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher


class StockAPI:
    def __init__(self):
        self.account = SimpleNamespace(id="seller", writes_enabled=False, automation_enabled=False)
        self.status, self.owner = "APPROVED", "seller"

    async def call(self, method, **kwargs):
        assert method == "get_item"
        return {
            "id": kwargs["id"],
            "name": "Stock product",
            "status": self.status,
            "user": {"id": self.owner},
        }


@pytest.fixture
async def rig(tmp_path):
    store, api, session = Store(tmp_path / "ui.sqlite"), StockAPI(), TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    store.claim_first_owner(7)
    ui = Interaction(bot, dispatcher(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    yield store, api, session, bot, ui, state
    await bot.session.close()
    store.close()


async def test_stock_input_preview_survives_restart_and_never_echoes_secrets(rig):
    store, api, session, _bot, ui, state = rig
    await ui.click("stock:add:item")
    await ui.message("first-private-unit\n\nfirst-private-unit\nsecond-private-unit")
    assert not store.rows("SELECT * FROM inventory")
    assert "новых единиц: 2" in session.calls[-1].text
    pending = (await state.get_data())["stock_pending"]
    ui.dp = dispatcher(store, api)
    await ui.click("stock:commit:" + pending["nonce"])
    await ui.click("stock:commit:" + pending["nonce"])
    assert Inventory(store, api).counts("item")["AVAILABLE"] == 2
    assert not store.rows("SELECT * FROM outbox")
    assert not store.rows("SELECT * FROM delivery_policies")
    for call in session.calls:
        assert "private-unit" not in (getattr(call, "text", None) or "")
    await ui.click("stock:list:item:AVAILABLE:0")
    assert "Единица №" in session.calls[-1].text
    before = len(store.rows("SELECT * FROM inventory"))
    await ui.click("stock:add:item", owner=8)
    await ui.message("foreign-private-unit", owner=8)
    assert len(store.rows("SELECT * FROM inventory")) == before


@pytest.mark.parametrize("change", ["status", "owner", "concurrent_import"])
async def test_stock_confirmation_rechecks_live_state_and_competing_import(rig, change):
    store, api, session, _bot, ui, state = rig
    await ui.click("stock:add:item")
    await ui.message("private-unit")
    nonce = (await state.get_data())["stock_pending"]["nonce"]
    if change == "status":
        api.status = "SOLD"
    elif change == "owner":
        api.owner = "another-seller"
    else:
        Orders(store).add_stock("item", ["private-unit"])
    await ui.click("stock:commit:" + nonce)
    assert "❗" in session.calls[-1].text
    assert len(store.rows("SELECT * FROM inventory")) == int(change == "concurrent_import")


async def test_wizard_stock_transfer_atomic_one_time_and_policy_disabled(rig):
    store, api, _session, _bot, ui, state = rig
    ident = store.draft(7, {"delivery": "stock", "delivery_data": "private-unit\nprivate-unit\nsecond-unit"})
    store.execute("INSERT INTO bindings(draft_id,remote_id) VALUES(?,?)", (ident, "item"))
    await ui.click("stock:transfer:" + ident)
    pending = (await state.get_data())["stock_pending"]
    await ui.click("stock:commit:" + pending["nonce"])
    assert Inventory(store, api).counts("item")["AVAILABLE"] == 2
    assert not Inventory(store, api).automation.get("item")[1].enabled
    assert Inventory(store, api).automation.get("item")[1].mode == "stock"
    assert len(store.rows("SELECT * FROM draft_stock_imports")) == 1
    await ui.click("stock:transfer:" + ident)
    assert len(store.rows("SELECT * FROM inventory")) == 2
    assert not store.rows("SELECT * FROM outbox")


async def test_reimport_never_reuses_reserved_delivered_or_quarantined_units(rig):
    store, api, session, _bot, ui, state = rig
    Orders(store).add_stock("item", ["reserved", "delivered", "quarantined"])
    for value, status in [
        ("reserved", "RESERVED"),
        ("delivered", "DELIVERED"),
        ("quarantined", "QUARANTINED"),
    ]:
        store.execute("UPDATE inventory SET status=? WHERE secret=?", (status, value))
    store.set_setting("delivery:item:enabled", False)
    await ui.click("stock:add:item")
    await ui.message("reserved\ndelivered\nquarantined\nfresh")
    nonce = (await state.get_data())["stock_pending"]["nonce"]
    await ui.click("stock:commit:" + nonce)
    assert Inventory(store, api).counts("item") == {
        "AVAILABLE": 1,
        "RESERVED": 1,
        "DELIVERED": 1,
        "QUARANTINED": 1,
    }
    assert store.setting("delivery:item:enabled") is False
    assert "не возобновляет" in session.calls[-1].text


async def test_explicit_stock_resume_requires_confirmation_and_preserves_runtime_flags(rig):
    store, api, _session, _bot, ui, state = rig
    await Inventory(store, api).automation.save(
        7,
        "item",
        0,
        asdict(DeliveryPolicy(enabled=True, mode="stock", auto_enroll=True)),
        future_confirmed=True,
    )
    store.set_setting("delivery:item:enabled", False)
    Orders(store).add_stock("item", ["private-unit"])
    await ui.click("stock:resume:item")
    assert store.setting("delivery:item:enabled") is False
    nonce = (await state.get_data())["stock_resume"]["nonce"]
    await ui.click("stock:resumeok:" + nonce)
    assert store.setting("delivery:item:enabled") is True
    assert api.account.writes_enabled is False and api.account.automation_enabled is False
