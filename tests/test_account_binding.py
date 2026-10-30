import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.__main__ import run
from app.database.store import Store
from app.playerok.adapter import APIError
from app.services.account_binding import AccountBindingError, bind_account
from app.services.orders import Orders
from app.services.transactions import Transactions
from tests.test_transactions import entry


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "binding.sqlite")
    yield value
    value.close()


def test_first_bind_and_repeated_verified_login_are_idempotent(store):
    bind_account(store, "seller")
    bind_account(store, "seller")
    assert store.setting("playerok:account_id") == "seller"
    assert store.setting("playerok:bound_account_id") == "seller"
    with pytest.raises(AccountBindingError, match="другому аккаунту"):
        bind_account(store, "other")
    assert store.setting("playerok:account_id") == "seller"


def test_concurrent_first_bind_commits_only_one_account(store):
    other = Store(store.path)

    def attempt(connection, account):
        try:
            bind_account(connection, account)
            return True
        except AccountBindingError:
            return False

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt, store, "first"), pool.submit(attempt, other, "second")]
            assert sorted(future.result() for future in futures) == [False, True]
        assert store.setting("playerok:account_id") == store.setting("playerok:bound_account_id")
        assert store.setting("playerok:account_id") in {"first", "second"}
    finally:
        other.close()


@pytest.mark.parametrize(
    "key", ["playerok:account_id", "transactions:account_id", "playerok:bound_account_id"]
)
def test_legacy_identity_conflict_is_not_overwritten(store, key):
    store.set_setting(key, "old")
    ident = store.enqueue("keep", "reply", "chat", {"text": "private"})
    with pytest.raises(AccountBindingError):
        bind_account(store, "new")
    assert store.setting(key) == "old"
    assert store.setting("playerok:bound_account_id") == ("old" if key.endswith("bound_account_id") else None)
    assert store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": "PENDING"}]


def test_transactions_and_known_sellers_prevent_false_legacy_adoption(store):
    Transactions(store, "seller").ingest(entry())
    with pytest.raises(AccountBindingError):
        bind_account(store, "other")
    bind_account(store, "seller")
    Orders(store).ingest(
        {
            "id": "deal",
            "direction": "OUT",
            "status": "PAID",
            "seller": {"id": "other"},
            "item": {"id": "product", "price": 10},
        }
    )
    with pytest.raises(AccountBindingError):
        bind_account(store, "seller")


def test_unattributed_old_queue_requires_review_and_fresh_empty_db_does_not(store):
    store.enqueue("old", "reply", "chat", {"text": "private"})
    with pytest.raises(AccountBindingError, match="не установлена"):
        bind_account(store, "seller")
    assert store.setting("playerok:account_id") is None


@pytest.mark.parametrize("value", [None, "", " seller", "seller ", [], 42])
def test_missing_or_invalid_verified_identity_is_rejected(store, value):
    with pytest.raises(AccountBindingError):
        bind_account(store, value)
    assert not store.rows("SELECT * FROM settings")


@pytest.mark.parametrize("value", [None, [], {}, 42, ""])
def test_corrupt_legacy_binding_is_not_reset(store, value):
    store.set_setting("playerok:account_id", value)
    with pytest.raises(AccountBindingError, match="Повреждена"):
        bind_account(store, "seller")
    assert store.setting("playerok:account_id") == value


@pytest.mark.parametrize("failure", ["different_account", "unauthorized"])
async def test_startup_unverified_account_retains_local_panel_without_write_permission(
    store, monkeypatch, failure
):
    store.set_setting("playerok:account_id", "old")
    account = SimpleNamespace(id="new", close=Mock(), writes_enabled=True)
    api = SimpleNamespace(account=account, call=AsyncMock(return_value={"id": "new"}))
    if failure == "unauthorized":
        # An account object with stale ID must not count as successful authentication.
        api.call.side_effect = APIError("UNAUTHORIZED_OR_CHALLENGE")
    session = SimpleNamespace(middleware=Mock(), close=AsyncMock())
    bot = SimpleNamespace(
        get_me=AsyncMock(), session=session, get_webhook_info=AsyncMock(return_value=SimpleNamespace(url=""))
    )
    backup = Mock()
    monkeypatch.setattr(store, "backup", backup)
    monkeypatch.setattr(store, "close", Mock())
    monkeypatch.setattr("app.__main__.Store", lambda path: store)
    monkeypatch.setattr("app.__main__.SafeAccount", lambda *a, **k: account)
    monkeypatch.setattr("app.__main__.Playerok", lambda account: api)
    monkeypatch.setattr("app.__main__.AiohttpSession", lambda **k: session)
    monkeypatch.setattr("app.__main__.Bot", lambda *a, **k: bot)
    monkeypatch.setattr("app.__main__.Supervisor.start", lambda *a: asyncio.create_task(asyncio.sleep(3600)))
    polling = AsyncMock()
    monkeypatch.setattr("app.__main__.Dispatcher.start_polling", polling)
    config = SimpleNamespace(
        database_path=store.path,
        playerok_header="test",
        writes_enabled=True,
        automation_enabled=False,
        use_proxy=False,
        telegram_token="test",
        owner_ids=(),
        realtime_enabled=False,
        first_start_claim=False,
    )
    await run(config)
    polling.assert_awaited_once()
    assert account.writes_enabled is False
    assert account.automation_enabled is False
    assert store.setting("playerok:account_id") == "old"
    assert store.setting("playerok:bound_account_id") is None
    backup.assert_called_once()
    api.call.assert_awaited_once_with("get")
    session.close.assert_awaited_once()
    account.close.assert_called_once()
