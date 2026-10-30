import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot

from app.database.store import Store
from app.playerok.adapter import APIError, plain
from app.services.transactions import Transactions, transaction_statistics
from app.workers.transactions import TransactionWorker
from playerokapi.parser import transaction
from tests.test_telegram_integration import CatalogAPI, Interaction, TelegramSession, dispatcher

AT = datetime(2026, 10, 30, 12, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "transactions.sqlite")
    yield value
    value.close()


def entry(ident="t", **changes):
    return {
        "id": ident,
        "operation": "SELL",
        "direction": "IN",
        "status": "CONFIRMED",
        "value": "90.00",
        "created_at": "2026-10-25T10:00:00Z",
        "completed_at": AT.isoformat(),
        "user": {"id": "seller"},
        "props": {"deal_id": "deal"},
        **changes,
    }


def page(entries, after=None):
    return {"transactions": entries, "page_info": {"has_next_page": after is not None, "end_cursor": after}}


def active(store):
    store.set_setting("transactions:account_id", "seller")
    return Transactions(store, "seller")


def test_journal_is_idempotent_scoped_and_discards_payment_credentials(store):
    service = active(store)
    secret = "DO_NOT_PERSIST_PAYMENT_CREDENTIALS"
    payload = entry(
        props={"deal_id": "deal", "payment_account": {"value": secret}, "payment_url": secret, "fee": 6}
    )
    assert service.ingest(payload)
    assert service.ingest(payload)
    Transactions(store, "other").ingest(entry(user={"id": "other"}, value=1000))
    rows = store.rows("SELECT * FROM account_transactions")
    assert len(rows) == 2
    assert secret not in json.dumps(rows)
    assert transaction_statistics(store, 1, AT)["sales_credit"] == 90
    assert rows[0]["deal_id"] == "deal"
    with pytest.raises(APIError, match="ACCOUNT_MISMATCH"):
        service.ingest(entry(user={"id": "attacker"}))
    assert len(store.rows("SELECT * FROM account_transactions")) == 2


def test_parser_preserves_new_operation_status_direction_without_guessing():
    raw = plain(
        transaction(
            {"id": "new", "operation": "NEW_OPERATION", "direction": "SIDEWAYS", "status": "HELD_NEW"}
        )
    )
    assert raw["operation"] == "NEW_OPERATION"
    assert raw["direction"] == "SIDEWAYS"
    assert raw["status"] == "HELD_NEW"
    assert (
        plain(transaction({"operation": "SELL", "direction": "IN", "status": "CONFIRMED"}))["operation"]
        == "SELL"
    )


def test_statistics_completed_date_not_created_date_and_exact_rolling_bounds(store):
    service = active(store)
    service.ingest(entry("start", completed_at=(AT - timedelta(days=1)).isoformat()))
    service.ingest(entry("at", completed_at="2026-10-30T15:00:00+03:00"))
    service.ingest(entry("before", completed_at="2026-10-29T11:59:59Z"))
    service.ingest(entry("future", completed_at="2026-10-30T12:00:01Z"))
    service.ingest(entry("undated", completed_at=None))
    service.ingest(entry("naive", completed_at="2026-10-30T10:00:00"))
    stats = transaction_statistics(store, 1, AT)
    assert stats["cached"] == 6
    assert stats["confirmed"] == 2
    assert stats["sales_credit"] == stats["credits"] == 180
    assert stats["unknown_dates"] == 2
    assert stats["coverage_start"] == "2026-10-29T11:59:59+00:00"
    assert stats["coverage_end"] == AT.isoformat()


def test_only_confirmed_known_rub_and_directions_are_summed(store):
    service = active(store)
    for status in ["PENDING", "PROCESSING", "ROLLED_BACK", "FAILED", "NEW_STATUS"]:
        service.ingest(entry(status, status=status, value=999))
    for value in [None, "NaN", "-10", "1e500"]:
        service.ingest(entry(str(value), value=value))
    service.ingest(entry("foreign", currency="USD", value=999))
    service.ingest(entry("unknown_direction", direction="UNKNOWN", value=999))
    service.ingest(entry("known"))
    stats = transaction_statistics(store, 1, AT)
    assert stats["credits"] == stats["sales_credit"] == 90
    assert stats["debits"] == 0
    assert stats["unknown_amounts"] == 4
    assert stats["foreign_currency"] == stats["unknown_directions"] == 1


def test_operation_buckets_do_not_turn_deposits_or_withdrawals_into_profit(store):
    service = active(store)
    for operation, direction, value in [
        ("SELL", "IN", 90),
        ("DEPOSIT", "IN", 500),
        ("REFERRAL_BONUS", "IN", 10),
        ("WITHDRAW", "OUT", 100),
        ("ITEM_PREMIUM_PRIORITY", "OUT", 25),
        ("ITEM_CUSTOM_PRIORITY", "OUT", 15),
        ("BUY", "OUT", 70),
    ]:
        service.ingest(
            entry(
                operation,
                operation=operation,
                direction=direction,
                value=value,
                fee=20,
                props={"fee": 6, "amount_after_conversion_rub": 94},
            )
        )
    stats = transaction_statistics(store, 1, AT)
    assert stats["credits"] == 600
    assert stats["sales_credit"] == 90
    assert stats["deposits"] == 500
    assert stats["debits"] == 210
    assert stats["withdraw_debit"] == 100
    assert stats["premium_debit"] == 25
    assert stats["other_priority_debit"] == 15
    assert stats["withdraw_net_reported"] == 94
    assert "profit" not in stats and "commissions" not in stats
    assert not store.rows("SELECT * FROM financial_operations")
    assert not store.rows("SELECT * FROM promotion_expenses")


def test_sparse_dates_preserved_stale_pending_ignored_rollback_removes_confirmed_credit(store):
    service = active(store)
    service.ingest(entry())
    assert not service.ingest(entry(status="PENDING", value=1000))
    service.ingest(entry(created_at=None, completed_at=None))
    assert (
        store.rows("SELECT created_at FROM account_transactions")[0]["created_at"]
        == "2026-10-25T10:00:00+00:00"
    )
    assert transaction_statistics(store, 1, AT)["credits"] == 90
    service.ingest(entry(status="ROLLED_BACK"))
    assert transaction_statistics(store, 1, AT)["credits"] == 0


async def test_worker_bounded_cursor_resume_head_refresh_and_restart(store):
    api = SimpleNamespace(
        account=SimpleNamespace(id="seller"),
        call=AsyncMock(
            side_effect=[
                page([entry("one")], "a"),
                page([entry("new"), entry("one")], "b"),
                page([entry("older")]),
            ]
        ),
    )
    worker = TransactionWorker(store, api, pages_per_cycle=1)
    await worker.once()
    assert worker.health["status"] == "BACKFILL"
    assert not store.setting("transactions:seller:completed_at")
    worker = TransactionWorker(store, api, pages_per_cycle=1)
    await worker.once()
    assert worker.health["status"] == "OK"
    assert store.setting("transactions:seller:completed_at")
    assert [call.kwargs["after_cursor"] for call in api.call.call_args_list] == [None, None, "a"]
    assert len(store.rows("SELECT * FROM account_transactions")) == 3
    assert all(call.args == ("get_transactions",) for call in api.call.call_args_list)
    assert not store.rows("SELECT * FROM outbox")


async def test_partial_commit_replays_without_duplicate_after_schema_failure(store):
    api = SimpleNamespace(
        account=SimpleNamespace(id="seller"),
        call=AsyncMock(return_value=page([entry("one"), entry("two", user={"id": "wrong"})], "next")),
    )
    worker = TransactionWorker(store, api)
    await worker.once()
    assert worker.health["code"] == "TRANSACTION_ACCOUNT_MISMATCH"
    assert len(store.rows("SELECT * FROM account_transactions")) == 1
    assert not store.rows("SELECT * FROM sync_cursors")
    api.call.return_value = page([entry("one"), entry("two")])
    store.set_setting("transactions:seller:not_before", 0)
    await TransactionWorker(store, api).once()
    assert len(store.rows("SELECT * FROM account_transactions")) == 2


async def test_no_account_and_rate_limit_preserve_health_and_cursor(store):
    api = SimpleNamespace(account=SimpleNamespace(id=None), call=AsyncMock())
    worker = TransactionWorker(store, api)
    await worker.once()
    assert worker.health["status"] == "NOT_AUTHORIZED"
    api.call.assert_not_called()
    api.account.id = "seller"
    api.call.side_effect = [page([entry()], "next"), APIError("HTTP_429", retry_after="120")]
    await worker.once()
    assert worker.health["code"] == "HTTP_429"
    assert store.setting("transactions:account_id") == "seller"
    assert not store.setting("transactions:seller:completed_at")
    assert "next" in store.rows("SELECT cursor FROM sync_cursors")[0]["cursor"]
    calls = api.call.await_count
    worker = TransactionWorker(store, api)
    await worker.once()
    assert worker.health["status"] == "BACKOFF"
    assert api.call.await_count == calls


async def test_account_change_during_empty_page_cannot_advance_checkpoint(store):
    api = SimpleNamespace(account=SimpleNamespace(id="seller"))

    async def call(*args, **kwargs):
        api.account.id = "different"
        return page([])

    api.call = call
    worker = TransactionWorker(store, api)
    await worker.once()
    assert worker.health["code"] == "TRANSACTION_ACCOUNT_CHANGED"
    assert not store.rows("SELECT * FROM sync_cursors")
    assert not store.setting("transactions:seller:completed_at")
    assert not store.setting("transactions:account_id")


async def test_dispatcher_shows_transaction_coverage_and_limits_without_api_call(store, monkeypatch):
    active(store).ingest(entry())
    monkeypatch.setattr(
        "app.telegram.statistics_panel.transaction_statistics",
        lambda s, days: transaction_statistics(s, days, AT),
    )
    api, session = CatalogAPI(), TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    try:
        await ui.message("/start")
        await ui.click("sd:7:transactions")
        text = session.calls[-1].text
        assert "за продажи: 90.00 ₽" in text
        assert "по дате завершения" in text
        assert "fee не суммируется" in text
        assert "полнота не гарантирована" in text
        assert len(text.encode("utf-16-le")) // 2 < 4096
        assert not api.calls
    finally:
        await dp.storage.close()
        await bot.session.close()
