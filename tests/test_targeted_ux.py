"""Targeted owner UX: real Dispatcher/Store/Outbox, external API boundaries only."""

import copy
import json

import pytest
from aiogram import Bot

from app.playerok.adapter import APIError
from app.services.logical_products import LogicalProducts
from tests.stateful_telegram import StatefulTelegram
from tests.test_gift_bundles import setup
from tests.test_gifts import rig as gift_rig
from tests.test_telegram_integration import Interaction
from tests.test_telegram_transport import production_dispatcher


@pytest.fixture
async def legacy(tmp_path, monkeypatch):
    for r in gift_rig.__wrapped__(tmp_path):
        r.api.item["price"] = 100
        await setup(r, "zombie_bear")
        await r.worker.once()  # Historical confirmed delivery through the production worker, fake MTProto.
        r.service.disarm(7, "item")
        r.store.set_setting("playerok:account_id", "shop")
        registry = LogicalProducts(r.store, r.api)
        monkeypatch.setattr("app.services.product_codes.secrets.choice", lambda _: "A")
        registry.migrate_legacy()
        r.registry = registry
        r.session = StatefulTelegram()
        r.bot = Bot("123:" + "x" * 35, session=r.session)
        r.ui = Interaction(r.bot, production_dispatcher(r.store, r.api))
        r.edits = []
        r.lost = False

        def update_item(id, r=r, **changes):
            r.edits.append((id, changes))
            r.api.item.update(changes)
            if r.lost:
                raise APIError("NETWORK_ERROR", uncertain=True)
            return copy.deepcopy(r.api.item)

        r.api.account.update_item = update_item
        yield r
        await r.session.close()


def action(r, suffix):
    return next(
        b.callback_data
        for row in r.session.last_panel.reply_markup.inline_keyboard
        for b in row
        if (b.callback_data or "").endswith(suffix)
    )


@pytest.mark.parametrize("lost", [False, True])
async def test_legacy_description_one_confirmation_no_payment_or_history_rewrite(legacy, lost):
    r = legacy
    tables = ["gift_listings", "gift_jobs", "gift_steps", "orders", "operation_receipts"]
    before = {t: r.store.rows("SELECT * FROM " + t) for t in tables}
    config = r.registry.listing("item")["config"]
    paid = len(r.remote.payments)
    rpc_count = len(r.remote.calls)
    await r.ui.message("/start")
    await r.ui.click("lp:item:item")
    migrate = action(r, ":migrate")
    await r.ui.click(migrate)
    assert "[AAA]" in r.session.last_panel.text
    assert "gift: zombie_bear" not in r.session.last_panel.text
    assert not r.edits
    confirm = action(r, ":commit")
    r.ui.dp = production_dispatcher(r.store, r.api)  # Persisted preview survives restart.
    r.lost = lost
    await r.ui.click(confirm)
    await r.ui.click(confirm)
    await r.worker.once()
    assert len(r.edits) == 1
    assert r.edits[0][0] == "item"
    assert r.edits[0][1]["description"] == "Real bundle"
    assert r.edits[0][1]["name"].endswith(" [AAA]")
    product = r.registry.listing("item")
    assert product["state"] == ("PENDING" if lost else "VERIFIED")
    assert product["enabled"] == 0 and product["config"] == config
    if lost:
        assert r.store.rows("SELECT status FROM outbox WHERE kind='edit'") == [{"status": "UNCERTAIN"}]
        await r.ui.click("lp:item:item")
        assert not any(
            (b.callback_data or "").endswith(":migrate")
            for row in r.session.last_panel.reply_markup.inline_keyboard
            for b in row
        )
        await r.ui.click(action(r, ":reconcile"))
        assert r.registry.listing("item")["state"] == "VERIFIED"
    assert not await r.worker.once()
    assert len(r.edits) == 1 and len(r.remote.payments) == paid
    assert len(r.remote.calls) == rpc_count
    for table in tables[:-1]:
        assert r.store.rows("SELECT * FROM " + table) == before[table]
    assert (
        r.store.rows(
            "SELECT * FROM operation_receipts WHERE operation_id IN (SELECT id FROM outbox WHERE kind!='edit')"
        )
        == before["operation_receipts"]
    )


@pytest.mark.parametrize("change", ["owner", "not_editable", "tag"])
async def test_legacy_preview_rejects_conflicting_source(legacy, change):
    r = legacy
    await r.ui.message("/start")
    await r.ui.click("lp:item:item")
    migrate = action(r, ":migrate")
    if change == "owner":
        r.api.item["user"]["id"] = "other"
    elif change == "not_editable":
        r.api.item["is_editable"] = False
    else:
        r.api.item["description"] = "Real bundle\ngift: heart"
    await r.ui.click(migrate)
    assert not r.edits
    assert not r.store.rows("SELECT * FROM outbox WHERE kind='edit'")
    assert r.registry.listing("item")["state"] == "LEGACY"


@pytest.fixture
async def financial(tmp_path):
    from app.database.store import Store
    from tests.test_finance_panel import FinancialAPI

    store = Store(tmp_path / "finance-ui.sqlite")
    store.claim_first_owner(7)
    api = FinancialAPI()
    api.item.update(status="APPROVED", price=149, raw_price=149, priority="PREMIUM", is_editable=True)
    store.set_setting("finance:7:cost:item", "20.00")
    session = StatefulTelegram()
    bot = Bot("123:" + "x" * 35, session=session)
    from types import SimpleNamespace

    r = SimpleNamespace(
        store=store, api=api, session=session, bot=bot, ui=Interaction(bot, production_dispatcher(store, api))
    )
    yield r
    await session.close()
    store.close()


async def test_single_sale_calculator_sbp_below_minimum_and_live_item_premium_quote(financial):
    r = financial
    await r.ui.message("/start")
    await r.ui.click("providers")
    await r.ui.click("fin:select:SBP")
    await r.ui.click("finance:item")
    text = r.session.last_panel.text
    assert "объединении" not in text and "неизвестно" not in text
    assert "Оценка комиссии СБП" in text and "8.04" in text
    assert "Ниже минимума" in text and "процентная часть" in text
    assert "PREMIUM" in text and "37.00" in text and "новый тариф" in text
    assert "Прибыль до дополнительных расходов: 114.00 ₽" in text
    assert "get_item_priority_statuses" in r.api.calls
    assert not r.store.rows("SELECT * FROM outbox")
    await r.ui.click("fin:details:item")
    assert "Историческая прибыль" in r.session.last_panel.text
    r.ui.dp = production_dispatcher(r.store, r.api)
    await r.ui.click("finance:item")
    assert "8.04" in r.session.last_panel.text


@pytest.mark.parametrize(
    "field,expected",
    [("editname", "Financial fixture"), ("editdesc", "Current confirmed description"), ("editprice", "149")],
)
async def test_edit_input_displays_current_server_value(financial, field, expected):
    r = financial
    r.api.item["description"] = "Current confirmed description"
    await r.ui.message("/start")
    await r.ui.click(field + ":item")
    assert expected in r.session.last_panel.text
    assert not r.store.rows("SELECT * FROM outbox")


async def test_edit_characteristics_shows_public_current_value_not_secret(financial):
    from tests.test_item_fields import FieldsAPI

    r = financial
    r.api = FieldsAPI()
    r.ui.dp = production_dispatcher(r.store, r.api)
    await r.ui.message("/start")
    await r.ui.click("editfields:item")
    assert "eu" in r.session.last_panel.text
    data = json.loads(r.store.rows("SELECT data FROM fsm")[0]["data"])
    await r.ui.click(f"w:{data['view_token']}:choose:0")
    assert "private old data" not in r.session.last_panel.text


@pytest.mark.parametrize("failure", [False, True])
async def test_cost_save_deletes_only_numeric_owner_message_and_survives_delete_failure(financial, failure):
    from aiogram.exceptions import TelegramNetworkError

    r = financial
    original = r.session.make_request
    deleted = []

    async def request(bot, method, timeout=None):
        if method.__api_method__ == "deleteMessage":
            deleted.append((method.chat_id, method.message_id))
            if failure:
                raise TelegramNetworkError(method=method, message="isolated delete timeout")
            return True
        return await original(bot, method, timeout)

    r.session.make_request = request
    await r.ui.message("/start")
    await r.ui.click("fin:cost:item")
    await r.ui.message("50,25")
    assert r.store.setting("finance:7:cost:item") == "50.25"
    assert deleted == [(7, r.ui.counter)]
    assert "50.25" in r.session.last_panel.text
    assert not r.store.rows("SELECT * FROM outbox")


async def test_moscow_dates_and_distinct_draft_labels(financial):
    from app.services.finance import checked_time
    from app.telegram.presentation import local_time

    assert local_time("2026-11-01T18:38:00+00:00") == "01.11.2026 в 21:38"
    assert checked_time("2026-11-01T18:38:00+00:00") == "01.11.2026 в 21:38"
    r = financial
    original = r.api.call

    async def api(method, **kwargs):
        if method == "get_my_items":
            return {"items": [], "page_info": {"has_next_page": False}}
        return await original(method, **kwargs)

    r.api.call = api
    await r.ui.message("/start")
    await r.ui.click("items")
    labels = [b.text for row in r.session.last_panel.reply_markup.inline_keyboard for b in row]
    assert "💾 Черновики" in labels
    await r.ui.click("itemfilter:DRAFT")
    assert "Черновики · локально и на Playerok" in r.session.last_panel.text


async def test_legacy_server_change_after_preview_prevents_description_write(legacy):
    r = legacy
    await r.ui.message("/start")
    await r.ui.click("lp:item:item")
    await r.ui.click(action(r, ":migrate"))
    confirm = action(r, ":commit")
    r.api.item["description"] = "Changed by owner elsewhere\ngift: zombie_bear"
    await r.ui.click(confirm)
    await r.worker.once()
    assert not r.edits
    assert r.store.rows("SELECT status,started_at FROM outbox WHERE kind='edit'") == [
        {"status": "PERMANENT_FAILED", "started_at": None}
    ]
    assert not await r.worker.once()


async def test_premium_history_is_distinct_from_new_quote_and_not_charged_per_sale(financial):
    r = financial
    # Historical server receipt fixture, materialized through the same Store outcome
    # writer. This test exercises UI accounting, not payment execution.
    for key, status in [("confirmed-premium", "SUCCEEDED"), ("unknown-premium", "UNCERTAIN")]:
        ident = r.store.enqueue(key, "premium_bump", "item", {}, actor=7)
        r.store.execute(
            "INSERT INTO promotion_expenses VALUES(?, 'item', '39', 'RESERVED', '2026-11-01T10:00:00+00:00')",
            (key,),
        )
        op = r.store.claim(operation_id=ident)
        r.store.mark_started(op)
        r.store.finish(ident, op["lease_token"], status, result={"payment_id": key, "paid_rub": "39.00"})
    before = r.store.rows("SELECT * FROM promotion_expenses")
    await r.ui.message("/start")
    await r.ui.click("finance:item")
    text = r.session.last_panel.text
    assert "новый тариф: 37.00 ₽" in text
    assert "Ранее оплачено PREMIUM/поднятий: 39.00 ₽ разово" in text
    assert "Прибыль до дополнительных расходов: 114.00 ₽" in text
    r.api.item["raw_price"] = 155
    original = r.api.call

    async def changed(method, **kwargs):
        value = await original(method, **kwargs)
        if method == "get_item_priority_statuses":
            value[1]["price"] = 41
        return value

    r.api.call = changed
    await r.ui.click("finance:item")
    assert "новый тариф: 41.00 ₽" in r.session.last_panel.text
    assert "39.00 ₽ разово" in r.session.last_panel.text
    assert r.store.rows("SELECT * FROM promotion_expenses") == before
