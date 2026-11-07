from dataclasses import asdict
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Store, now
from app.services.automation import DeliveryPolicy
from app.services.inventory import Inventory
from app.services.orders import Orders
from app.services.product_readiness import ProductReadiness
from app.telegram.storage import SQLiteStorage
from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher


class StockAPI:
    def __init__(self):
        self.account = SimpleNamespace(id="seller", writes_enabled=False, automation_enabled=False)
        self.status, self.owner = "APPROVED", "seller"

    async def call(self, method, **kwargs):
        if method == "get_deal":
            return self.order
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


@pytest.mark.parametrize(
    "condition,expected",
    [
        ("ready", "Включено, есть блокировка"),
        ("runtime", "Включено, есть блокировка"),
        ("baseline", "Включено, есть блокировка"),
        ("empty", "Включено, есть блокировка"),
        ("guard", "Остановлено"),
        ("disabled", "Выключено владельцем"),
    ],
)
async def test_item_order_and_policy_share_actual_readiness(rig, condition, expected):
    store, api, session, _bot, ui, _state = rig
    automation = Inventory(store, api).automation
    api.account.writes_enabled = api.account.automation_enabled = True
    store.set_setting("playerok:account_id", "seller")
    store.set_setting("orders:last_complete_sync", now())
    await automation.save(
        7,
        "item",
        0,
        asdict(DeliveryPolicy(enabled=condition != "disabled", mode="stock", auto_enroll=True)),
        future_confirmed=True,
    )
    if condition != "empty":
        Orders(store).add_stock("item", ["private-unit"])
    if condition == "runtime":
        api.account.automation_enabled = False
    if condition == "baseline":
        store.set_setting("orders:last_complete_sync", None)
    if condition == "guard":
        store.set_setting("delivery:item:enabled", False)
    api.order = {
        "id": "order",
        "status": "PAID",
        "direction": "OUT",
        "item": {"id": "item", "user": {"id": "seller"}},
        "chat": {"id": "chat"},
    }
    readiness = ProductReadiness(store, api).snapshot("item", item=await api.call("get_item", id="item"))
    assert readiness["state"] == (
        "OFF" if condition == "disabled" else "STOPPED" if condition == "guard" else "BLOCKED"
    )
    assert "BINDING" in {problem["code"] for problem in readiness["problems"]}
    for action in ("item:item", "order:order", "auto:item:item"):
        await ui.click(action)
        text = session.calls[-1].text
        compact = (
            "выключена — отправка остановлена"
            if condition == "disabled"
            else "приостановлена"
            if condition == "guard"
            else "ожидает устранения проблемы"
        )
        if action.startswith("order:"):
            # Order cards now describe the actual wait/STOP, rather than
            # repeating the product-wide diagnostic vocabulary.
            order_expected = {
                "ready": "Способ выдачи настроен",
                "runtime": "запрещена настройками подключения",
                "baseline": "Бот загружает подтверждённые данные",
                "empty": "Склад пуст",
                "guard": "остановлена владельцем",
                "disabled": "остановлена владельцем",
            }[condition]
            assert order_expected.lower() in text.lower()
        else:
            assert compact.lower() in text.lower()
        assert "Автодействия: выключены" not in text
        if action.startswith("order:"):
            assert "Требует внимания:" not in text
        if action.startswith("auto:"):
            assert "со склада" in text.lower()
        # Full reasons and stock remain available from the nested delivery screen.
        if action.startswith("item:"):
            # The compact card keeps the same state; complete reasons are one
            # explicit read-only check away rather than repeated on every card.
            await ui.click("rd:check:item")
            assert "Постоянная привязка не подтверждена" in session.calls[-1].text
            await ui.click("item:item")
            await ui.click("iv:delivery:item")
            assert compact in session.calls[-1].text.lower() and "со склада" in session.calls[-1].text.lower()
    assert not store.rows("SELECT * FROM outbox")
    assert not store.rows("SELECT * FROM reservations")


async def test_stock_resume_does_not_reenable_after_last_unit_reserved_during_api_check(rig):
    store, api, session, _bot, ui, state = rig
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
    nonce = (await state.get_data())["stock_resume"]["nonce"]
    original = api.call

    async def reserve_during_check(method, **kwargs):
        result = await original(method, **kwargs)
        store.execute("UPDATE inventory SET status='RESERVED'")
        return result

    api.call = reserve_during_check
    await ui.click("stock:resumeok:" + nonce)
    assert store.setting("delivery:item:enabled") is False
    assert "❗" in session.calls[-1].text


async def test_failed_draft_transfer_rolls_back_inventory_and_marker(rig):
    store, api, session, _bot, ui, state = rig
    ident = store.draft(7, {"delivery": "stock", "delivery_data": "private-unit"})
    store.execute("INSERT INTO bindings(draft_id,remote_id) VALUES(?,?)", (ident, "item"))
    await ui.click("stock:transfer:" + ident)
    nonce = (await state.get_data())["stock_pending"]["nonce"]
    store.execute(
        "CREATE TRIGGER fail_transfer BEFORE INSERT ON draft_stock_imports BEGIN SELECT RAISE(ABORT,'fixture interruption'); END"
    )
    await ui.click("stock:commit:" + nonce)
    assert "Изменения транзакции отменены" in session.calls[-1].text
    assert not store.rows("SELECT * FROM inventory")
    assert not store.rows("SELECT * FROM draft_stock_imports")
    assert not store.rows("SELECT * FROM delivery_policies")
    store.execute("DROP TRIGGER fail_transfer")
    await ui.click("stock:commit:" + nonce)
    assert Inventory(store, api).counts("item")["AVAILABLE"] == 1


async def test_sync_error_blocks_ready_label(rig):
    store, api, _session, _bot, _ui, _state = rig
    api.account.writes_enabled = api.account.automation_enabled = True
    store.set_setting("orders:last_complete_sync", now())
    store.set_setting("sync:health", {"status": "ERROR"})
    service = Inventory(store, api).automation
    await service.save(
        7,
        "item",
        0,
        asdict(DeliveryPolicy(enabled=True, mode="stock", auto_enroll=True)),
        future_confirmed=True,
    )
    Orders(store).add_stock("item", ["private-unit"])
    assert service.readiness("item")["status"] == "Не готово"
