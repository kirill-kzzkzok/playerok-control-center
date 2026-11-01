from html import unescape

import pytest
from aiogram import Bot

from app.database.store import Store
from app.services.orders import Orders
from app.services.statistics import statistics
from tests.test_statistics import AT
from tests.test_telegram_integration import CatalogAPI, Interaction, TelegramSession, dispatcher


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "bestsellers.sqlite")
    yield value
    value.close()


def sale(
    store,
    ident,
    item,
    amount=10,
    *,
    status="PAID",
    created="2026-10-30T10:00:00Z",
    name=None,
    category="<Game>",
):
    Orders(store).ingest(
        {
            "id": ident,
            "direction": "OUT",
            "status": status,
            "created_at": created,
            "item": {
                "id": item,
                "name": name or f"<{item}>",
                "price": amount,
                "category": {"name": category},
            },
        }
    )


def test_paid_order_counts_include_unknown_money_and_do_not_merge_same_named_items(store):
    for index in range(4):
        sale(store, f"unknown{index}", "unknown", None, name="Shared name")
    for index in range(3):
        sale(store, f"cheap{index}", "cheap", 10)
    sale(store, "expensive", "expensive", 1000, name="Shared name")
    result = statistics(store, 1, AT)
    assert result["item_orders"] == {"unknown": 4, "cheap": 3, "expensive": 1}
    assert result["item_unknown_paid"] == {"unknown": 4}
    assert result["items"] == {"cheap": 30, "expensive": 1000}
    assert result["category_orders"] == {"<Game>": 8}
    assert result["category_unknown_paid"] == {"<Game>": 4}
    assert result["categories"] == {"<Game>": 1030}


def test_volume_ranking_uses_same_period_and_paid_current_status_as_revenue(store):
    for status in ["PAID", "SENT", "CONFIRMED", "CONFIRMED_AUTOMATICALLY"]:
        sale(store, status, "included", status=status)
    for status in ["ROLLED_BACK", "PENDING", "FAILED"]:
        sale(store, status, "excluded", status=status)
    for ident, date in [
        ("previous", "2026-10-29T11:59:59Z"),
        ("future", "2026-10-30T12:00:01Z"),
        ("missing", None),
        ("naive", "2026-10-30T10:00:00"),
    ]:
        sale(store, ident, "excluded", created=date)
    result = statistics(store, 1, AT)
    assert result["item_orders"] == {"included": 4}
    assert result["category_orders"] == {"<Game>": 4}
    assert result["previous_paid"] == 1
    assert result["unknown_dates"] == 2


def test_latest_server_order_name_wins_independent_of_insert_order_and_offset(store):
    sale(store, "latest", "item", name="Latest", created="2026-10-30T14:00:00+03:00")
    sale(store, "older", "item", name="Older", created="2026-10-30T10:00:00Z")
    sale(store, "tie-z", "item", name="Tie z", created="2026-10-30T11:00:00Z")
    sale(store, "tie-a", "item", name="Tie a", created="2026-10-30T14:00:00+03:00")
    assert statistics(store, 1, AT)["item_names"] == {"item": "Tie z"}


@pytest.mark.parametrize("days", [1, 2, 7, 30, 0])
async def test_dispatcher_ranks_counts_before_revenue_and_exposes_unknown_without_network(
    store, monkeypatch, days
):
    monkeypatch.setattr(
        "app.telegram.statistics_panel.grouped_statistics",
        lambda db, owner, account, period: statistics(db, period, AT),
    )
    sale(store, "expensive", "expensive", 999)
    sale(store, "small1", "small", 5)
    sale(store, "small2", "small", None)
    sale(store, "zero", "zero", 0)
    sale(store, "unknown", "unknown", None)
    session, api = TelegramSession(), CatalogAPI()
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    try:
        await ui.message("/start")
        await ui.click(f"sd:{days}:bestsellers")
        text = session.calls[-1].text
        assert text.index("&lt;small&gt;") < text.index("&lt;expensive&gt;")
        assert "Заказов: 2 · 5.00 ₽ · без суммы: 1" in text
        assert "нет известных сумм · без суммы: 1" in text
        assert "Заказов: 1 · 0.00 ₽" in text
        assert "не единиц товара" in text
        assert "возвраты исключены" in text
        await ui.click(f"sd:{days}:category_counts")
        assert "&lt;Game&gt;" in session.calls[-1].text
        assert "Заказов: 5 · 1004.00 ₽ · без суммы: 2" in session.calls[-1].text
        # Rankings are grouped under the actual seller-facing top-products menu.
        await ui.click(f"sr:{days}:top")
        buttons = {
            button.callback_data for row in session.calls[-1].reply_markup.inline_keyboard for button in row
        }
        assert {
            f"sd:{days}:bestsellers",
            f"sd:{days}:items",
            f"sd:{days}:category_counts",
            f"sd:{days}:categories",
        } <= buttons
        await ui.click(f"sd:{days}:items")
        assert session.calls[-1].text.index("&lt;expensive&gt;") < session.calls[-1].text.index(
            "&lt;small&gt;"
        )
        assert not api.calls
        assert not store.rows("SELECT * FROM outbox")
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_top15_ties_are_stable_and_long_names_fit_telegram_text_limit(store, monkeypatch):
    monkeypatch.setattr(
        "app.telegram.statistics_panel.grouped_statistics",
        lambda db, owner, account, period: statistics(db, period, AT),
    )
    for index in reversed(range(20)):
        sale(store, f"order{index:02}", f"item{index:02}", None, name="<&>" * 100)
    session, api = TelegramSession(), CatalogAPI()
    bot = Bot("123:" + "x" * 35, session=session)
    dp = dispatcher(store, api)
    ui = Interaction(bot, dp)
    try:
        await ui.message("/start")
        await ui.click("sd:0:bestsellers")
        text = session.calls[-1].text
        assert "item14" not in text and "item15" not in text  # IDs stay out of the normal ranking
        assert "15." in text and "16." not in text
        assert statistics(store, 0, AT)["item_orders"]["item14"] == 1
        assert len(unescape(text)) < 4096
        assert "&lt;&amp;&gt;" in text
        await ui.click("sd:0:bestsellers", owner=8)
        assert session.calls[-1].text == "Доступ закрыт"
        assert not api.calls
    finally:
        await dp.storage.close()
        await bot.session.close()
