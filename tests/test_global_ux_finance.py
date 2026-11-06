"""Seller journeys through the production Dispatcher; no live transports."""

from copy import deepcopy

import pytest

from tests.test_ux_redesign import buttons
from tests.test_ux_redesign import ux as ux_fixture
from tests.test_withdrawals import snapshot


@pytest.fixture(name="ux")
async def _ux(tmp_path):
    async for value in ux_fixture.__wrapped__(tmp_path):
        yield value


async def test_main_items_contains_only_one_verified_active_offer(ux):
    r = ux
    original = r.api.call
    values = [
        {
            **deepcopy(r.api.value),
            "id": str(i),
            "name": "Call of Duty" if i == 0 else "Old gift",
            "status": "APPROVED" if i == 0 else ["DRAFT", "SOLD", "EXPIRED"][i % 3],
        }
        for i in range(8)
    ]

    async def call(method, **kwargs):
        if method == "get_my_items":
            r.api.calls.append((method, kwargs))
            return {"items": values, "page_info": {"has_next_page": False}}
        return await original(method, **kwargs)

    r.api.call = call
    await r.ui.click("items")
    labels = buttons(r.session.calls[-1])
    assert [k for k in labels if k.startswith("item:")] == ["item:0"]
    assert r.api.calls[-1][1]["statuses"][0].name == "APPROVED"


async def test_ten_text_navigation_clicks_use_one_panel(ux):
    r = ux
    for action in [
        "more",
        "settings",
        "home",
        "stats:7",
        "sd:7:details",
        "stats:7",
        "sr:7:top",
        "more",
        "settings",
        "home",
    ]:
        await r.ui.click(action)
    assert sum(m.__api_method__ == "sendMessage" for m in r.session.calls) == 1
    assert not r.store.rows("SELECT * FROM outbox")


async def test_global_withdrawal_selection_never_requests_amount(ux):
    r = ux
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_transaction_providers":
            return [snapshot(minimum=1)["provider"]]
        return await original(method, **kwargs)

    r.api.call = call
    await r.ui.click("providers")
    await r.ui.click("fin:select:SBP")
    assert await r.state.get_state() != "finance:amount"
    assert "Введите сумму" not in r.session.calls[-1].text
    assert r.store.setting("finance:7:withdrawal")["provider"]["id"] == "SBP"


async def test_item_exposes_net_calculator_on_primary_card(ux):
    await ux.ui.click("item:lot")
    assert buttons(ux.session.calls[-1])["finance:lot"] == "💰 Расчёт прибыли"


async def test_per_sale_forecast_quote_matches_display(ux):
    import json

    from app.services.products import Products
    from app.telegram.finance_panel import FinancePanel
    from tests.test_finance_panel import FinancialAPI

    api = FinancialAPI()
    ux.store.set_setting("finance:7:withdrawal", snapshot(minimum=1))
    ux.store.set_setting("finance:7:cost:item", "5.00")
    ux.store.set_setting("finance:7:placement_per_sale:item", "10.00")
    panel = FinancePanel(ux.store, api, Products(ux.store, api))
    text, _ = await panel.item(7, "item")
    row = ux.store.rows("SELECT payload FROM financial_quotes ORDER BY rowid DESC LIMIT 1")[0]
    quote = json.loads(row["payload"])
    assert quote["inputs"]["placement"] == "10.00"
    assert quote["result"]["profit"] is not None
    assert "Прибыль до дополнительных расходов: 1795.00 ₽" in text
    details, _ = await panel.item(7, "item", details=True)
    assert quote["result"]["profit"] + " ₽" in details


async def test_close_then_start_creates_one_new_panel(ux):
    from aiogram.exceptions import TelegramBadRequest

    deleted = set()
    original = ux.session.make_request

    async def strict(bot, method, timeout=None):
        ident = getattr(method, "message_id", None)
        if method.__api_method__ == "deleteMessage":
            deleted.add(ident)
        elif method.__api_method__.startswith("editMessage") and ident in deleted:
            raise TelegramBadRequest(method=method, message="message to edit not found")
        return await original(bot, method, timeout)

    ux.session.make_request = strict
    await ux.ui.click("nav:close")
    count = sum(m.__api_method__ == "sendMessage" for m in ux.session.calls)
    await ux.ui.message("/start")
    assert sum(m.__api_method__ == "sendMessage" for m in ux.session.calls) == count + 1
    assert not ux.store.setting("navigation:123:7")["closed"]
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_statistics_back_restores_actual_origin_period(ux):
    await ux.ui.click("stats:30")
    await ux.ui.click("sd:30:details")
    action = next(k for k in buttons(ux.session.calls[-1]) if k.startswith("nav:back:"))
    await ux.ui.click(action)
    assert "30 дн." in ux.session.calls[-1].text
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_explicit_offer_group_aggregates_ids_without_merging_names(ux):
    from app.database.store import now
    from app.services.orders import Orders
    from app.services.statistics import confirm_offer_group, grouped_statistics

    for i in range(4):
        Orders(ux.store).ingest(
            {
                "id": "sale" + str(i),
                "status": "CONFIRMED",
                "direction": "OUT",
                "created_at": now(),
                "item": {
                    "id": "offer" + str(i),
                    "name": "Call of Duty",
                    "price": 90,
                    "user": {"id": "seller"},
                    "category": {"id": "games", "name": "Games"},
                },
            }
        )
    ux.store.set_setting("playerok:account_id", "seller")
    before = ux.store.rows("SELECT * FROM orders ORDER BY id")
    confirm_offer_group(ux.store, 7, "seller", ["offer0", "offer1", "offer2"])
    result = grouped_statistics(ux.store, 7, "seller", 7)
    assert sorted(result["item_orders"].values()) == [1, 3]
    assert sorted(result["items"].values()) == [90, 270]
    assert ux.store.rows("SELECT * FROM orders ORDER BY id") == before


async def test_offer_group_confirmation_runs_through_dispatcher_and_survives_restart(ux):
    from app.database.store import now
    from app.services.orders import Orders
    from tests.test_telegram_integration import dispatcher

    ux.store.set_setting("playerok:account_id", "seller")
    for i in range(3):
        Orders(ux.store).ingest(
            {
                "id": str(i),
                "status": "CONFIRMED",
                "direction": "OUT",
                "created_at": now(),
                "item": {
                    "id": "offer" + str(i),
                    "name": "Call of Duty",
                    "price": 90,
                    "user": {"id": "seller"},
                    "category": {"id": "games", "name": "Games"},
                },
            }
        )
    await ux.ui.click("sg:7:list")
    await ux.ui.message("1, 2, 3")
    action = next(k for k in buttons(ux.session.calls[-1]) if k.startswith("sg:7:commit:"))
    ux.ui.dp = dispatcher(ux.store, ux.api)
    await ux.ui.click(action)
    await ux.ui.click("sd:7:bestsellers")
    text = ux.session.calls[-1].text
    assert text.count("Call of Duty") == 1
    assert "Заказов: 3" in text and "270.00 ₽" in text
    await ux.ui.click("sd:7:items")
    assert ux.session.calls[-1].text.count("Call of Duty") == 1
    assert "270.00 ₽" in ux.session.calls[-1].text
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_old_emergency_stop_cannot_mutate_from_unknown_navigation_send(ux):
    panel = ux.store.setting("navigation:123:7")
    panel.update(sending=True, pending_kind="text")
    ux.store.set_setting("navigation:123:7", panel)
    stopped = ux.store.setting("gifts:stopped")
    revision = ux.store.setting("gifts:stop_revision")
    await ux.ui.click("gft:stop", message_id=777)
    assert ux.store.setting("gifts:stopped") == stopped
    assert ux.store.setting("gifts:stop_revision") == revision
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_back_recovers_confirmed_callback_after_lost_panel_ack(ux):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.types import InlineKeyboardMarkup

    from tests.test_telegram_transport import production_dispatcher

    ux.ui.dp = production_dispatcher(ux.store, ux.api)
    await ux.ui.click("stats:7")
    await ux.ui.click("st:7:time")
    original = ux.session.make_request
    failed = False

    async def fail_once(bot, method, timeout=None):
        nonlocal failed
        if method.__api_method__ == "sendMessage" and not failed:
            failed = True
            ux.session.calls.append(method)
            raise TelegramNetworkError(method=method, message="Request timeout error")
        return await original(bot, method, timeout)

    ux.session.make_request = fail_once
    await ux.ui.click("stats:7")
    panel = ux.store.setting("navigation:123:7")
    assert panel["sending"]
    markup = InlineKeyboardMarkup.model_validate(panel["pending_markup"])
    back = next(
        b.callback_data
        for row in markup.inline_keyboard
        for b in row
        if (b.callback_data or "").startswith("nav:back:")
    )
    sends = sum(m.__api_method__ == "sendMessage" for m in ux.session.calls)
    await ux.ui.click(back, message_id=444, reply_markup=markup)
    assert not ux.store.setting("navigation:123:7")["sending"]
    assert ux.store.setting("navigation:123:7")["id"] == 444
    assert sum(m.__api_method__ == "sendMessage" for m in ux.session.calls) == sends
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_item_uses_exact_obtaining_fee_without_unrelated_schema_requests(ux):
    from app.services.products import Products
    from app.telegram.finance_panel import FinancePanel
    from tests.test_finance_panel import FinancialAPI

    api = FinancialAPI()
    api.item.pop("fee_multiplier")
    api.item.update(category={"id": "category"}, obtaining_type={"id": "specific", "fee_multiplier": "0.25"})
    text, _ = await FinancePanel(ux.store, api, Products(ux.store, api)).item(7, "item")
    assert "Комиссия Playerok: 500.00 ₽" in text
    assert api.calls == ["get_item", "get_item_priority_statuses"]


async def test_global_withdrawal_profile_item_back_flow_uses_fresh_method(ux):
    original = ux.api.call

    async def call(method, **kwargs):
        if method == "get":
            return {"id": "seller", "username": "FixtureOwner", "profile": {}, "is_blocked": False}
        if method == "get_transaction_providers":
            value = snapshot(minimum=1)["provider"]
            value.update(fee=6, name="СБП")
            return [value]
        return await original(method, **kwargs)

    ux.api.call = call
    ux.api.value.update(price=2000, fee_multiplier="0.1")
    await ux.ui.click("profile")
    await ux.ui.click("providers")
    await ux.ui.click("fin:select:SBP")
    assert await ux.state.get_state() is None
    await ux.ui.click("item:lot")
    await ux.ui.click("finance:lot")
    assert "На руки после вывода: 1692.00 ₽" in ux.session.last_panel.text
    back = next(k for k in buttons(ux.session.last_panel) if k.startswith("nav:back:"))
    await ux.ui.click(back)
    assert "Цена: 2000 ₽" in ux.session.last_panel.text
    await ux.ui.click("profile")
    assert "Получение денег: СБП" in ux.session.last_panel.text
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_explicit_navigation_reset_does_not_restore_money_confirmation(ux):
    from aiogram.exceptions import TelegramNetworkError

    from tests.test_telegram_transport import production_dispatcher

    ux.ui.dp = production_dispatcher(ux.store, ux.api)
    await ux.ui.click("st:7:time")
    original = ux.session.make_request
    failed = False

    async def fail_once(bot, method, timeout=None):
        nonlocal failed
        if method.__api_method__ == "sendMessage" and not failed:
            failed = True
            ux.session.calls.append(method)
            raise TelegramNetworkError(method=method, message="Request timeout error")
        return await original(bot, method, timeout)

    ux.session.make_request = fail_once
    await ux.ui.click("stats:7")
    assert ux.store.setting("navigation:123:7")["sending"]
    await ux.ui.message("/panel_reset")
    recover = next(k for k in buttons(ux.session.last_panel) if k.startswith("nav:recover:"))
    await ux.ui.click(recover)
    assert not ux.store.setting("navigation:123:7").get("sending")
    assert await ux.state.get_data() == {}
    assert set(buttons(ux.session.last_panel)) == {
        "items",
        "orders",
        "chats",
        "promotions",
        "stats:7",
        "settings",
    }
    assert not ux.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("variant", ["foreign", "category", "unknown_category", "changed_title"])
async def test_offer_group_rejects_unconfirmed_or_different_product(ux, variant):
    from app.database.store import now
    from app.services.orders import Orders
    from app.services.statistics import confirm_offer_group

    ux.store.set_setting("playerok:account_id", "seller")
    for i in range(2):
        value = {
            "id": str(i),
            "name": "Same",
            "price": 90,
            "user": {"id": "seller"},
            "category": {"id": "games"},
        }
        if i:
            if variant == "foreign":
                value["user"]["id"] = "other"
            if variant == "category":
                value["category"]["id"] = "other"
            if variant == "unknown_category":
                value["category"] = {}
            if variant == "changed_title":
                value["name"] = "Different"
        Orders(ux.store).ingest(
            {"id": str(i), "status": "CONFIRMED", "direction": "OUT", "created_at": now(), "item": value}
        )
    with pytest.raises(ValueError):
        confirm_offer_group(ux.store, 7, "seller", ["0", "1"])
    assert ux.store.setting("analytics:7:seller:offer_groups") is None
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_explicitly_saved_filter_wins_over_later_unsaved_selection(ux):
    await ux.ui.click("items")
    await ux.ui.click("itemfilter:DRAFT")
    await ux.ui.click("il:remember")
    await ux.ui.click("itemfilter:APPROVED")
    await ux.ui.click("home")
    await ux.ui.click("items")
    assert (await ux.state.get_data())["item_filters"]["status"] == "DRAFT"
    assert ux.store.setting("ui:7:item_filters")["status"] == "DRAFT"
    assert not ux.store.rows("SELECT * FROM outbox")
