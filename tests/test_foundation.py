import asyncio
import logging
from dataclasses import replace

import httpx
import pytest
from aiogram.fsm.storage.base import StorageKey

from app.config import Config, ConfigurationError
from app.database.store import Conflict, Store
from app.playerok.adapter import APIError, SafeAccount, parse_cookies
from app.security.logging import SecretFilter
from app.telegram.storage import SQLiteStorage


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "test.sqlite")
    yield value
    value.close()


def test_no_implicit_owner():
    config = Config("123:" + "x" * 30, "token=hidden")
    with pytest.raises(ConfigurationError):
        config.require_owner()
    replace(config, owner_ids=frozenset({42})).require_owner()


def test_secrets_not_in_repr():
    assert "hidden" not in repr(Config("hidden", "hidden"))


def test_cookie_preserves_equals():
    assert parse_cookies("token=abc==; other=x=y")["token"] == "abc=="


def test_logging_filter():
    record = logging.LogRecord("test", 20, "", 1, "url bot123:secret token=abc== %s", ("hidden",), None)
    SecretFilter(["hidden"]).filter(record)
    assert "secret" not in record.getMessage() and "abc" not in record.getMessage()
    assert "hidden" not in record.getMessage()


async def test_fsm_survives_restart(store):
    key = StorageKey(bot_id=1, chat_id=2, user_id=2)
    storage = SQLiteStorage(store)
    await storage.set_state(key, "Wizard:name")
    await asyncio.gather(storage.update_data(key, {"a": 1}), storage.update_data(key, {"b": 2}))
    new = Store(store.path)
    try:
        assert await SQLiteStorage(new).get_state(key) == "Wizard:name"
        assert await SQLiteStorage(new).get_data(key) == {"a": 1, "b": 2}
    finally:
        new.close()


def test_optimistic_revision(store):
    ident = store.draft(1, {"name": "one"})
    store.update_draft(ident, 1, 0, {"name": "two"})
    with pytest.raises(Conflict):
        store.update_draft(ident, 1, 0, {})
    with pytest.raises(Conflict):
        store.get_draft(ident, 2)


def test_backup_preserves_data(store, tmp_path):
    store.set_setting("a", {"v": 1})
    path = tmp_path / "backup.sqlite"
    store.backup(path)
    backup = Store(path)
    try:
        assert backup.setting("a") == {"v": 1}
    finally:
        backup.close()


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_errors_are_safe_and_never_retried(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, text="sensitive upstream body")

    account = SafeAccount("token=secret", client=httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(APIError) as error:
        account.request("post", account.base_url + "/graphql", {}, {"operationName": "viewer"})
    assert len(calls) == 1
    assert "sensitive" not in str(error.value)


def test_writes_and_foreign_endpoints_blocked_before_network():
    def handle(request):
        pytest.fail("must not reach network")

    account = SafeAccount("token=secret", client=httpx.Client(transport=httpx.MockTransport(handle)))
    for operation in ["publishItem", "requestWithdrawal", "increaseItemPriorityStatus", "sendMessage"]:
        with pytest.raises(APIError):
            account.request("post", account.base_url + "/graphql", {}, {"operationName": operation})
    with pytest.raises(APIError):
        account.request("get", "https://example.com/graphql", {}, {"operationName": "viewer"})
