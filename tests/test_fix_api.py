"""Bounded API contracts and positive field evidence, no real HTTP."""

from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Store
from app.playerok.adapter import APIError, Playerok, plain
from app.services.products import Products
from app.telegram.storage import SQLiteStorage
from app.telegram.wizard import Wizard
from app.workers.outbox import Executor, OutboxWorker
from tests.test_item_fields import FieldsAPI, click


async def test_endless_fresh_cursor_is_bounded_and_never_returns_complete():
    api = Playerok(SimpleNamespace())
    calls = 0

    async def fetch(method, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 101:
            raise AssertionError("unbounded paging")
        return {"items": [{"id": str(calls)}], "page_info": {"has_next_page": True, "end_cursor": str(calls)}}

    api.call = fetch
    with pytest.raises(APIError, match="PAGINATION_LIMIT"):
        async for _ in api.pages("get_my_items", "items"):
            pass
    assert calls <= 100


async def test_field_edit_with_silent_server_ignored_values_is_not_success(tmp_path):
    store = Store(tmp_path / "fields.sqlite")
    api = FieldsAPI()
    api.account.update_item = lambda **kw: SimpleNamespace(id=kw["id"])  # Server silently ignores fields.
    wizard = Wizard(store, api, Products(store, api))
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=1, user_id=1))
    try:
        await wizard.start_edit(1, state, "item")
        await click(wizard, state, "choose:1")
        await wizard.message(SimpleNamespace(from_user=SimpleNamespace(id=1), text="replacement data"), state)
        await click(wizard, state, "submit")
        await OutboxWorker(store, Executor(store, api, Products(store, api))).once()
        assert store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
        assert store.rows("SELECT state FROM drafts")[0]["state"] != "UPDATED"
    finally:
        store.close()


@pytest.mark.parametrize("code", ["CONFIRMATION", "RESTRICTION"])
def test_agreement_icon_keeps_wire_identity_despite_upstream_enum_alias(code):
    from playerokapi.parser import game_category_agreement

    parsed = plain(
        game_category_agreement({"id": "agreement", "description": "Seller terms", "iconType": code})
    )
    assert parsed["icon_type_code"] == code


@pytest.mark.parametrize("host", ["149.154.167.220", "playerok.com"])
def test_offline_boundary_blocks_external_connections(host):
    import socket

    with (
        socket.socket() as client,
        pytest.raises(AssertionError, match="OFFLINE_TEST_EXTERNAL_NETWORK_FORBIDDEN"),
    ):
        client.connect((host, 443))
