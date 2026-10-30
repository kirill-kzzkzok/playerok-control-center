from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from matplotlib import pyplot as plt
from PIL import Image

from app.database.store import Store
from app.playerok.adapter import APIError
from app.rendering.charts import revenue_chart, smooth_points, temporal_curve
from app.services.orders import Orders
from app.services.statistics import statistics
from app.workers.sync import Synchronizer
from tests.test_telegram_integration import CatalogAPI, Interaction, TelegramSession, dispatcher

AT = datetime(2026, 10, 30, 12, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "statistics.sqlite")
    yield value
    value.close()


def order(store, ident, status="PAID", amount=100, created="2026-10-30T10:00:00+00:00"):
    Orders(store).ingest(
        {
            "id": ident,
            "direction": "OUT",
            "status": status,
            "created_at": created,
            "item": {"id": "product", "name": "<Product>", "price": amount, "category": {"name": "<Game>"}},
        }
    )


def test_periods_use_server_dates_and_aware_boundaries(store):
    order(store, "start", amount=200, created="2026-10-29T15:00:00+03:00")
    order(store, "at", created=AT.isoformat())
    order(store, "previous", amount=50, created="2026-10-28T12:00:00+00:00")
    order(store, "before", created="2026-10-28T11:59:59+00:00")
    order(store, "future", created="2026-10-30T12:00:01+00:00")
    for ident, created in [("missing", None), ("naive", "2026-10-30T10:00:00"), ("bad", "invalid")]:
        order(store, ident, created=created)
    stats = statistics(store, 1, AT)
    assert stats["orders"] == stats["paid"] == 2
    assert stats["revenue"] == 300
    assert stats["previous_revenue"] == 50
    assert stats["previous_paid"] == 1
    assert stats["growth_percent"] == 500
    assert stats["unknown_dates"] == 3
    assert stats["coverage_start"] == "2026-10-28T11:59:59+00:00"
    assert stats["coverage_end"] == AT.isoformat()
    assert stats["hourly_orders"]["2026-10-29 15:00"] == 1
    assert stats["basis"] == "created_order_cohort_current_status"
    all_time = statistics(store, 0, AT)
    assert all_time["orders"] == 4
    assert all_time["growth_percent"] is None


def test_unknown_pending_does_not_suppress_paid_average_but_unknown_paid_does(store):
    order(store, "paid", amount="101.01")
    order(store, "pending", "PENDING", None)
    stats = statistics(store, 7, AT)
    assert stats["unknown_amounts"] == 1
    assert stats["unknown_paid_amounts"] == 0
    assert stats["average"] == Decimal("101.01")
    assert stats["daily_average"] == {"2026-10-30": Decimal("101.01")}
    order(store, "unknown_paid", "SENT", "NaN")
    stats = statistics(store, 7, AT)
    assert stats["average"] is None
    assert stats["daily_average"] == {}
    assert stats["paid"] == 2
    assert stats["unknown_paid_amounts"] == 1
    assert stats["revenue"] == Decimal("101.01")


def test_previous_unknown_disables_growth_and_current_status_is_used(store):
    order(store, "previous", amount=None, created="2026-10-29T11:59:59+00:00")
    order(store, "current", amount=200)
    stats = statistics(store, 1, AT)
    assert stats["previous_paid"] == stats["previous_unknown_paid"] == 1
    assert stats["previous_average"] is stats["growth_percent"] is None
    order(store, "current", "CONFIRMED", 200)
    order(store, "refund", "ROLLED_BACK", 50)
    order(store, "failed", "FAILED", 10)
    stats = statistics(store, 1, AT)
    assert stats["orders"] == 3
    assert stats["confirmed_revenue"] == stats["revenue"] == 200
    assert stats["refunds"] == 50
    assert stats["items"] == {"product": Decimal(200)}
    assert stats["item_names"] == {"product": "<Product>"}
    assert stats["category_orders"] == {"<Game>": 1}


def test_expense_sources_not_combined_or_estimates_treated_as_charges(store):
    with store.transaction() as db:
        for ident, kind, amount, currency, when in [
            ("known", "EXPENSE", "30", "RUB", AT.isoformat()),
            ("foreign", "EXPENSE", "999", "USD", AT.isoformat()),
            ("invalid", "EXPENSE", "NaN", "RUB", AT.isoformat()),
            ("income", "INCOME", "999", "RUB", AT.isoformat()),
            ("future", "EXPENSE", "999", "RUB", "2027-01-01T00:00:00+00:00"),
        ]:
            db.execute(
                "INSERT INTO financial_operations VALUES(?,?,?,?,?,?)",
                (ident, kind, amount, currency, when, "{}"),
            )
        for status in ["RESERVED", "UNCERTAIN", "SUCCEEDED", "ESTIMATE_ONLY"]:
            db.execute(
                "INSERT INTO promotion_expenses VALUES(?,?,?,?,?)",
                (status, "product", "10", status, AT.isoformat()),
            )
    stats = statistics(store, 1, AT)
    assert stats["expenses"] == 30
    assert stats["promotion_expenses"] == 10
    assert stats["promotion_daily"] == {"2026-10-30": Decimal(10)}
    assert stats["unknown_expenses"] == 2


@pytest.mark.parametrize("days", [1, 2, 7, 30, 0])
def test_chart_views_empty_and_populated(store, days):
    for populated in [False, True]:
        if populated:
            order(store, "current")
        stats = statistics(store, days, AT)
        for view in ["overview", "time", "average", "promotion", "comparison"]:
            image = revenue_chart(stats, view)
            with Image.open(BytesIO(image)) as picture:
                assert picture.format == "PNG"
                assert picture.width > 1000
                assert picture.height >= 500


def test_invalid_period_or_naive_clock_rejected(store):
    with pytest.raises(ValueError):
        statistics(store, 3, AT)
    with pytest.raises(ValueError):
        statistics(store, 1, AT.replace(tzinfo=None))


def test_irregular_history_has_temporal_ticks_and_does_not_bridge_missing_days():
    figure, axis = plt.subplots()
    try:
        temporal_curve(axis, {"2024-01-01": 10, "2024-01-02": 20, "2026-10-29": 5})
        figure.canvas.draw()
        assert len(axis.lines) == 2
        assert max(axis.lines[0].get_xdata()) - min(axis.lines[0].get_xdata()) == 1
        assert len(axis.get_xticks()) <= 8
    finally:
        plt.close(figure)


@pytest.mark.parametrize("values", [[0, 10, 0, 20, 5], [0, 0, 20, 20, 0], [0, 1, 2, 3, 4], [100, 0, 0, 0, 1]])
def test_smooth_curves_preserve_observations_and_never_invent_peaks(values):
    x = [0, 1, 2, 10, 11]
    curve_x, curve_y = smooth_points(x, values)
    assert curve_x[::16] == x
    assert curve_y[::16] == values
    for index in range(len(x) - 1):
        segment = curve_y[index * 16 : (index + 1) * 16 + 1]
        assert min(segment) >= min(values[index : index + 2]) - 1e-10
        assert max(segment) <= max(values[index : index + 2]) + 1e-10
    assert smooth_points([], []) == ([], [])
    assert smooth_points([1], [7]) == ([1], [7])
    assert smooth_points([0, 1], [2, 4])[1][8] == 3
    with pytest.raises(ValueError):
        smooth_points([1, 1], [0, 1])


async def test_dispatcher_statistics_access_callbacks_and_escaping(store, monkeypatch):
    monkeypatch.setattr("app.telegram.statistics_panel.statistics", lambda db, days: statistics(db, days, AT))
    order(store, "sale")
    session, api = TelegramSession(), CatalogAPI()
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    try:
        await ui.message("/start")
        session.calls.clear()
        await ui.click("stats:7", owner=8)
        assert len(session.calls) == 1
        assert session.calls[0].__api_method__ == "answerCallbackQuery"
        assert session.calls[0].show_alert
        for action in ["stats:1", "st:2:time", "st:7:average", "st:30:promotion", "st:0:comparison"]:
            await ui.click(action)
            sent = session.calls[-1]
            assert sent.__api_method__ == "sendPhoto"
            assert len(sent.caption) < 1024
            assert "прибыль не подтверждены" in sent.caption
        await ui.click("sd:0:details")
        assert "Период: всё время" in session.calls[-1].text
        assert "статусы текущие" in session.calls[-1].text
        await ui.click("sd:7:items")
        assert "&lt;Product&gt;" in session.calls[-1].text
        await ui.click("sd:7:categories")
        assert "&lt;Game&gt;" in session.calls[-1].text
        assert not api.calls
        assert not store.rows("SELECT * FROM outbox")
        assert not store.rows("SELECT * FROM promotion_expenses")
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_missing_dates_backfill_is_bounded_durable_and_survives_sparse_list(store):
    for ident in ["a", "b", "c"]:
        order(store, ident, created=None)
    api = SimpleNamespace(
        call=AsyncMock(
            return_value={
                "id": "a",
                "status": "PAID",
                "direction": "OUT",
                "created_at": "2026-10-30T10:00:00Z",
                "item": {"id": "product", "price": 100},
            }
        )
    )
    sync = Synchronizer(store, api, Orders(store))
    assert sync.queue_order_details(limit=2) == 2
    sync = Synchronizer(store, api, Orders(store))
    assert sync.queue_order_details(limit=2) == 1
    assert sync.queue_order_details(limit=2) == 0
    assert await sync.process_requests(limit=1) == 1
    api.call.assert_awaited_once_with("get_deal", deal_id="a")
    assert statistics(store, 1, AT)["unknown_dates"] == 2
    order(store, "a", created=None)
    stats = statistics(store, 1, AT)
    assert stats["unknown_dates"] == 2
    assert stats["orders"] == 1
    assert stats["revenue"] == 100
    # An undated successful detail response is tried once, not on every list scan.
    api.call.return_value = {
        "id": "b",
        "status": "PAID",
        "direction": "OUT",
        "item": {"id": "product", "price": 100},
    }
    assert await sync.process_requests(limit=1) == 1
    assert sync.queue_order_details() == 0
    assert not store.rows("SELECT * FROM outbox")


async def test_backfill_does_not_reset_existing_request_revision_or_error_backoff(store):
    order(store, "a", created=None)
    api = SimpleNamespace(call=AsyncMock(side_effect=APIError("HTTP_429", retry_after="120")))
    sync = Synchronizer(store, api, Orders(store))
    sync.request("deal", "a")
    sync.request("deal", "a")
    assert sync.queue_order_details() == 0
    await sync.process_requests()
    row = store.rows("SELECT * FROM sync_requests")[0]
    assert row["revision"] == 2
    assert row["completed_revision"] == 0
    assert row["failures"] == 1
    assert row["error_code"] == "HTTP_429"
    assert Synchronizer(store, api, Orders(store)).queue_order_details() == 0
    assert await sync.process_requests() == 0
    assert api.call.await_count == 1
