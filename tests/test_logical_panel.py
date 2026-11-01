"""Production panel/FSM and services, with test transports only at external boundaries."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Conflict, Store
from app.services.inventory import Inventory
from app.services.products import Products
from app.telegram.logical_products_panel import LogicalProductsPanel
from app.telegram.storage import SQLiteStorage
from app.workers.outbox import Executor, OutboxWorker
from tests.test_gifts import rig as gift_rig
from tests.test_logical_products import ReadAPI


@pytest.fixture
def panel(tmp_path):
    store = Store(tmp_path / "panel.sqlite3")
    store.claim_first_owner(7)
    store.set_setting("playerok:account_id", "seller")
    api = ReadAPI()
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    yield SimpleNamespace(store=store, api=api, state=state, panel=LogicalProductsPanel(store, api))
    store.close()


@pytest.fixture(name="gifts")
def _gifts(tmp_path):
    yield from gift_rig.__wrapped__(tmp_path)


def callbacks(markup):
    return {button.callback_data: button.text for row in markup.inline_keyboard for button in row}


async def choose(r, handler):
    await r.panel.callback("lp:item:lot", 7, r.state)
    nonce = (await r.state.get_data())["logical_view"]["nonce"]
    await r.panel.callback(f"lp:{nonce}:configure", 7, r.state)
    return nonce, await r.panel.callback(f"lp:{nonce}:mode:{handler}", 7, r.state)


async def test_fixed_text_ui_preview_queues_real_edit_and_requires_separate_arm(panel):
    r = panel
    nonce, _ = await choose(r, "fixed_text")
    text, _ = await r.panel.message(
        SimpleNamespace(text="private-delivery-text", from_user=SimpleNamespace(id=7)), r.state
    )
    assert "private-delivery-text" not in text
    assert not r.store.rows("SELECT * FROM outbox")
    text, markup = await r.panel.callback(f"lp:{nonce}:preview", 7, r.state)
    assert "private-delivery-text" not in text
    assert not r.store.rows("SELECT * FROM outbox")
    commit = next(key for key in callbacks(markup) if key.endswith(":commit"))
    text, _ = await r.panel.callback(commit, 7, r.state)
    assert "поставлено в очередь" in text
    assert r.store.rows("SELECT enabled FROM logical_products")[0]["enabled"] == 0
    with pytest.raises(Conflict):
        await r.panel.callback(commit, 7, r.state)
    await OutboxWorker(r.store, Executor(r.store, r.api, Products(r.store, r.api))).once()
    text, markup = await r.panel.callback("lp:item:lot", 7, r.state)
    assert "подтверждено Playerok" in text
    assert any(key.endswith(":arm") for key in callbacks(markup))
    assert len(r.api.mutations) == 1


async def test_stock_input_confirms_count_without_leaking_or_losing_setup(panel):
    r = panel
    nonce, _ = await choose(r, "unique_stock")
    await r.panel.callback(f"lp:{nonce}:stockinput", 7, r.state)
    text, markup = await r.panel.message(
        SimpleNamespace(text="private-one\nprivate-two\nprivate-one", from_user=SimpleNamespace(id=7)),
        r.state,
    )
    assert "private-" not in text
    assert "2" in text
    assert not r.store.rows("SELECT * FROM inventory")
    commit = next(key for key in callbacks(markup) if key.endswith(":stock_commit"))
    await r.panel.callback(commit, 7, r.state)
    assert Inventory(r.store, r.api).counts("lot")["AVAILABLE"] == 2
    assert (await r.state.get_data())["logical_setup"]["handler"] == "unique_stock"
    with pytest.raises(Conflict):
        await r.panel.callback(commit, 7, r.state)
    assert not r.store.rows("SELECT * FROM outbox")


async def test_cancelled_preview_cannot_be_committed_from_old_button(panel):
    r = panel
    nonce, _ = await choose(r, "fixed_text")
    await r.panel.message(SimpleNamespace(text="delivery", from_user=SimpleNamespace(id=7)), r.state)
    _, markup = await r.panel.callback(f"lp:{nonce}:preview", 7, r.state)
    commit = next(key for key in callbacks(markup) if key.endswith(":commit"))
    await r.panel.callback(commit.rsplit(":", 1)[0] + ":setup", 7, r.state)
    with pytest.raises(Conflict):
        await r.panel.callback(commit, 7, r.state)
    assert not r.store.rows("SELECT * FROM outbox")


async def test_cancelled_stock_preview_cannot_import_from_old_button(panel):
    r = panel
    nonce, _ = await choose(r, "unique_stock")
    await r.panel.callback(f"lp:{nonce}:stockinput", 7, r.state)
    _, markup = await r.panel.message(
        SimpleNamespace(text="private-unit", from_user=SimpleNamespace(id=7)), r.state
    )
    commit = next(key for key in callbacks(markup) if key.endswith(":stock_commit"))
    await r.panel.callback(commit.rsplit(":", 1)[0] + ":setup", 7, r.state)
    with pytest.raises(Conflict):
        await r.panel.callback(commit, 7, r.state)
    assert not r.store.rows("SELECT * FROM inventory")


async def test_dangerous_actions_require_their_own_visible_confirmation(panel):
    r = panel
    nonce, _ = await choose(r, "fixed_text")
    await r.panel.message(SimpleNamespace(text="delivery", from_user=SimpleNamespace(id=7)), r.state)
    _, markup = await r.panel.callback(f"lp:{nonce}:preview", 7, r.state)
    await r.panel.callback(next(key for key in callbacks(markup) if key.endswith(":commit")), 7, r.state)
    await OutboxWorker(r.store, Executor(r.store, r.api, Products(r.store, r.api))).once()
    await r.panel.callback("lp:item:lot", 7, r.state)
    nonce = (await r.state.get_data())["logical_view"]["nonce"]
    with pytest.raises(Conflict):
        await r.panel.callback(f"lp:{nonce}:enable:inherit", 7, r.state)
    with pytest.raises(Conflict):
        await r.panel.callback(f"lp:{nonce}:detach_confirm", 7, r.state)
    product = r.store.rows("SELECT * FROM logical_products")[0]
    assert product["state"] == "VERIFIED" and product["enabled"] == 0


async def test_prepared_panel_survives_recreated_sqlite_fsm(panel):
    r = panel
    nonce, _ = await choose(r, "fixed_text")
    await r.panel.message(
        SimpleNamespace(text="private-stored-delivery", from_user=SimpleNamespace(id=7)), r.state
    )
    _, markup = await r.panel.callback(f"lp:{nonce}:preview", 7, r.state)
    commit = next(key for key in callbacks(markup) if key.endswith(":commit"))
    restored = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    await LogicalProductsPanel(r.store, r.api).callback(commit, 7, restored)
    assert len(r.store.rows("SELECT * FROM outbox")) == 1
    assert not r.api.mutations
    with pytest.raises(Conflict):
        await r.panel.callback(commit, 8, restored)


async def test_gift_setup_respects_configured_count_limits_and_spends_nothing(gifts):
    r = gifts
    r.store.set_setting("playerok:account_id", "shop")
    r.api.item["price"] = 100
    r.api.gift_settings = replace(r.api.gift_settings, max_gifts=6)
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    panel = LogicalProductsPanel(r.store, r.api)
    await panel.callback("lp:item:item", 7, state)
    nonce = (await state.get_data())["logical_view"]["nonce"]
    await panel.callback(f"lp:{nonce}:configure", 7, state)
    await panel.callback(f"lp:{nonce}:mode:telegram_gifts", 7, state)
    await panel.message(
        SimpleNamespace(
            text="gift: heart, heart, heart, heart, heart, heart", from_user=SimpleNamespace(id=7)
        ),
        state,
    )
    await panel.callback(f"lp:{nonce}:input:order", 7, state)
    await panel.message(SimpleNamespace(text="90", from_user=SimpleNamespace(id=7)), state)
    await panel.callback(f"lp:{nonce}:input:day", 7, state)
    await panel.message(SimpleNamespace(text="100", from_user=SimpleNamespace(id=7)), state)
    text, _ = await panel.callback(f"lp:{nonce}:preview", 7, state)
    assert "90 ⭐" in text
    assert len((await state.get_data())["logical_prepared"]["config"]["bundle"]) == 6
    assert not r.remote.payments
    assert not r.store.rows("SELECT * FROM outbox")


async def test_explicit_enable_stop_and_confirmed_detach_preserve_listing(panel):
    from app.database.store import now

    r = panel
    r.store.set_setting("orders:last_complete_sync", now())
    nonce, _ = await choose(r, "fixed_text")
    await r.panel.message(SimpleNamespace(text="delivery", from_user=SimpleNamespace(id=7)), r.state)
    _, markup = await r.panel.callback(f"lp:{nonce}:preview", 7, r.state)
    await r.panel.callback(next(key for key in callbacks(markup) if key.endswith(":commit")), 7, r.state)
    await OutboxWorker(r.store, Executor(r.store, r.api, Products(r.store, r.api))).once()
    _, markup = await r.panel.callback("lp:item:lot", 7, r.state)
    _, markup = await r.panel.callback(
        next(key for key in callbacks(markup) if key.endswith(":arm")), 7, r.state
    )
    _, markup = await r.panel.callback(
        next(key for key in callbacks(markup) if key.endswith(":enable:inherit")), 7, r.state
    )
    product = r.store.rows("SELECT * FROM logical_products")[0]
    assert product["enabled"] == 1 and product["inherit_enabled"] == 1
    await r.panel.callback(next(key for key in callbacks(markup) if key.endswith(":stop")), 7, r.state)
    _, markup = await r.panel.callback("lp:item:lot", 7, r.state)
    _, markup = await r.panel.callback(
        next(key for key in callbacks(markup) if key.endswith(":detach")), 7, r.state
    )
    await r.panel.callback(
        next(key for key in callbacks(markup) if key.endswith(":detach_confirm")), 7, r.state
    )
    assert r.store.rows("SELECT state FROM logical_products")[0]["state"] == "DETACHED"
    assert len(r.api.mutations) == 1
    assert r.api.item["description"].endswith(product["code"])


async def test_gift_refund_toggle_is_explicit_local_policy_and_does_not_spend(gifts):
    r = gifts
    r.store.set_setting("playerok:account_id", "shop")
    r.api.item["price"] = 100
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    panel = LogicalProductsPanel(r.store, r.api)
    await panel.callback("lp:item:item", 7, state)
    nonce = (await state.get_data())["logical_view"]["nonce"]
    await panel.callback(f"lp:{nonce}:configure", 7, state)
    await panel.callback(f"lp:{nonce}:mode:telegram_gifts", 7, state)
    text, markup = await panel.message(
        SimpleNamespace(text="gift: heart", from_user=SimpleNamespace(id=7)), state
    )
    assert "Возврат при неверном username: выключен" in text
    assert not (await state.get_data())["logical_setup"]["config"].get("options", {}).get("refund", False)
    action = next(key for key in callbacks(markup) if key.endswith(":refund"))
    text, _ = await panel.callback(action, 7, state)
    assert "Возврат при неверном username: разрешён" in text
    assert "глобальное разрешение" in text
    assert (await state.get_data())["logical_setup"]["config"]["options"]["refund"] is True
    await panel.callback(action, 7, state)
    assert (await state.get_data())["logical_setup"]["config"]["options"]["refund"] is False
    assert not r.store.rows("SELECT * FROM outbox")
    assert not r.api.refunds and not r.api.sent and not r.remote.payments
