import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Conflict, Store, encode, now
from app.services.orders import Orders
from app.telegram.automation_queue import AutomationQueue
from app.telegram.storage import SQLiteStorage


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "queue.sqlite")
    store.claim_first_owner(1)
    api = SimpleNamespace(
        account=SimpleNamespace(id="seller"),
        call=AsyncMock(return_value={"id": "item", "user": {"id": "seller"}}),
    )
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=1, user_id=1))
    yield store, api, state, AutomationQueue(store, api)
    store.close()


def chain(store):
    Orders(store).ingest(
        {"id": "order", "status": "PAID", "direction": "OUT", "item": {"id": "item"}, "chat": {"id": "chat"}}
    )
    Orders(store).add_stock("item", ["private code"])
    store.execute("UPDATE inventory SET status='RESERVED'")
    inventory = store.rows("SELECT id FROM inventory")[0]["id"]
    store.execute("INSERT INTO reservations VALUES('order',?)", (inventory,))
    store.execute(
        "INSERT INTO delivery_chains VALUES('order','item',1,1,?,'ACTIVE',?)",
        (encode({"text": "private code"}), now()),
    )
    store.execute(
        "INSERT INTO delivery_chain_steps(order_id,position,kind,text) VALUES('order',0,'delivery_text','private code')"
    )


async def test_full_policy_and_target_pagination_no_duplicates(setup):
    store, api, state, panel = setup
    for index in range(27):
        store.execute("INSERT INTO delivery_policies VALUES(?,1,1,'{}',?)", (str(index), now()))
        store.execute("INSERT INTO promotion_targets VALUES(?,'exclude',1,?)", (str(index), now()))
    for kind in ("policies", "targets"):
        buttons = []
        for page in range(3):
            _, markup = await panel.callback(f"aq:list:{kind}:{page}", 1, state)
            buttons.extend(
                button.callback_data
                for row in markup.inline_keyboard
                for button in row
                if button.callback_data.startswith("auto:item:" if kind == "policies" else "aq:target:")
            )
        assert len(buttons) == len(set(buttons)) == 27
    api.call.assert_not_called()


async def test_chain_stop_persists_confirmation_and_quarantines_without_echo(setup):
    store, api, state, panel = setup
    chain(store)
    text, _ = await panel.callback("aq:chain:order", 1, state)
    assert "private" not in text
    await panel.callback("aq:stop:order", 1, state)
    nonce = (await state.get_data())["chain_stop"]["nonce"]
    # Fresh context/panel restores the same local confirmation.
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=1, user_id=1))
    await AutomationQueue(store, api).callback("aq:apply:" + nonce, 1, state)
    assert store.rows("SELECT status FROM delivery_chains")[0]["status"] == "CANCELLED"
    assert store.rows("SELECT status FROM inventory")[0]["status"] == "QUARANTINED"
    assert not store.rows("SELECT * FROM outbox")
    api.call.assert_not_called()
    with pytest.raises(Conflict):
        await panel.callback("aq:apply:" + nonce, 1, state)


async def test_changed_step_after_stop_preview_requires_new_confirmation(setup):
    store, _, state, panel = setup
    chain(store)
    await panel.callback("aq:stop:order", 1, state)
    nonce = (await state.get_data())["chain_stop"]["nonce"]
    ident = store.enqueue("chain:order:0", "chain_message", "order", {"text": "hello"})
    store.execute("UPDATE delivery_chain_steps SET operation_id=?", (ident,))
    with pytest.raises(Conflict, match="изменилась"):
        await panel.callback("aq:apply:" + nonce, 1, state)
    assert store.rows("SELECT status FROM delivery_chains")[0]["status"] == "ACTIVE"


async def test_stop_does_not_overwrite_inflight_external_result(setup):
    store, _, state, panel = setup
    chain(store)
    ident = store.enqueue("delivery:order", "delivery", "order", {"chain_id": "order"})
    store.execute("UPDATE delivery_chain_steps SET operation_id=?", (ident,))
    action = store.claim()
    await panel.callback("aq:stop:order", 1, state)
    nonce = (await state.get_data())["chain_stop"]["nonce"]
    await panel.callback("aq:apply:" + nonce, 1, state)
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "PROCESSING"
    store.finish(ident, action["lease_token"], "SUCCEEDED", result={"message_id": "accepted-before-stop"})
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "SUCCEEDED"
    assert store.rows("SELECT status FROM inventory")[0]["status"] == "DELIVERED"
    assert store.rows("SELECT status FROM delivery_chains")[0]["status"] == "CANCELLED"


async def test_foreign_owner_cannot_read_or_stop_or_reschedule(setup):
    store, api, state, panel = setup
    chain(store)
    for action in ("aq:list:chains:0", "aq:stop:order", "aq:recheck:item"):
        with pytest.raises(Conflict):
            await panel.callback(action, 2, state)
    api.call.assert_not_called()


async def test_reschedule_does_not_reset_active_lease_or_enable_policy(setup):
    store, _, state, panel = setup
    store.execute(
        "INSERT INTO promotion_schedule(item_id,due_at,lease_until,lease_token) VALUES('item',?,?, 'token')",
        (time.time() + 100, time.time() + 100),
    )
    with pytest.raises(Conflict, match="lease"):
        await panel.callback("aq:recheck:item", 1, state)
    assert store.rows("SELECT lease_token FROM promotion_schedule")[0]["lease_token"] == "token"
    store.execute("UPDATE promotion_schedule SET lease_until=0")
    await panel.callback("aq:recheck:item", 1, state)
    assert store.rows("SELECT due_at FROM promotion_schedule")[0]["due_at"] == 0
    assert not store.rows("SELECT * FROM promotion_policy")
    assert not store.rows("SELECT * FROM outbox")


async def test_evaluation_human_readable_escaped_without_exposing_raw_json(setup):
    store, _, state, panel = setup
    store.execute(
        "INSERT INTO promotion_evaluations(item_id,policy_revision,result,checked_at) VALUES('item',1,?,?)",
        (
            json.dumps(
                {"price": "10", "would_allow": False, "reasons": ["<blocked>"], "internal": "not for UI"}
            ),
            now(),
        ),
    )
    text, _ = await panel.callback("aq:evaluation:1", 1, state)
    assert "&lt;blocked&gt;" in text and "10 ₽" in text
    assert "not for UI" not in text
