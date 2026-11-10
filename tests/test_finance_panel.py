import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Store, now
from app.services.finance import calculate
from app.services.financial_quotes import save_quote
from app.services.products import Products
from app.telegram.finance_panel import FinancePanel
from app.telegram.storage import SQLiteStorage
from app.telegram.wizard import Wizard


class FinancialAPI:
    account = SimpleNamespace(id="seller", writes_enabled=False)

    def __init__(self):
        self.calls = []
        self.item = {
            "id": "item",
            "user": {"id": "seller"},
            "name": "Financial fixture",
            "is_editable": False,
            "price": 2000,
            "raw_price": 2000,
            "status": "SOLD",
            "fee_multiplier": "0.1",
        }

    async def call(self, method, **kwargs):
        self.calls.append(method)
        if method == "get_transaction_providers":
            return [
                {
                    "id": ident,
                    "name": ident,
                    "fee": 6,
                    "min_fee_amount": 60,
                    "limits": {"outgoing": {"min": 500, "max": 79000}},
                }
                for ident in ["LOCAL", "SBP", "USDT"]
            ]
        if method == "get_exchange_rate":
            return {"pair": "USDT_RUB", "rate": "100"}
        if method == "get_item":
            return deepcopy(self.item)
        if method == "get_item_priority_statuses":
            assert kwargs["item_price"] == self.item["raw_price"]
            return [
                {"id": "free", "name": "Бесплатно", "type": "DEFAULT", "price": 0},
                {"id": "premium", "name": "PREMIUM", "type": "PREMIUM", "price": 37},
            ]
        if method == "get_game_category":
            return {"id": "category", "options": [], "fee_multiplier": "0.1"}
        raise AssertionError(method)

    async def pages(self, *args, **kwargs):
        yield []


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "financial.sqlite")
    store.claim_first_owner(7)
    api = FinancialAPI()
    products = Products(store, api)
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=7, user_id=7))
    yield store, api, products, state, FinancePanel(store, api, products)
    store.close()


async def test_provider_selection_amount_and_restart_never_withdraw(setup):
    store, api, _, state, panel = setup
    _, markup = await panel.callback("providers", 7, state)
    assert all(button.callback_data != "fin:select:LOCAL" for row in markup.inline_keyboard for button in row)
    await panel.callback("fin:select:SBP", 7, state)
    state = FSMContext(SQLiteStorage(store), state.key)
    assert await state.get_state() is None
    text, _ = await panel.callback("finance:item", 7, state)
    assert "1692.00" in text and "Прогноз будущей продажи" in text
    assert store.setting("finance:7:withdrawal")["provider"]["id"] == "SBP"
    assert store.setting("finance:8:withdrawal") is None
    assert not store.rows("SELECT * FROM outbox")
    assert not store.rows("SELECT * FROM financial_operations")
    quotes = store.rows("SELECT * FROM financial_quotes")
    assert len(quotes) == 1 and json.loads(quotes[0]["payload"])["kind"] == "ESTIMATE_ONLY"
    await panel.callback("fin:refresh", 7, state)
    assert set(api.calls) <= {"get_transaction_providers", "get_item", "get_item_priority_statuses"}


async def test_local_cost_for_sold_item_does_not_require_market_editability(setup):
    store, api, _, state, panel = setup
    _, markup = await panel.callback("finance:item", 7, state)
    assert any(button.callback_data == "fin:cost:item" for row in markup.inline_keyboard for button in row)
    await panel.callback("fin:cost:item", 7, state)
    text, _ = await panel.message(SimpleNamespace(text="500,25", from_user=SimpleNamespace(id=7)), state)
    assert store.setting("finance:7:cost:item") == "500.25"
    assert "500.25" in text
    assert "Прибыль:" not in text  # promotion expense and withdrawal are unknown
    assert set(api.calls) == {"get_item", "get_item_priority_statuses"}
    assert not store.rows("SELECT * FROM outbox")


async def test_forged_provider_choice_and_foreign_item_are_rejected(setup):
    store, api, _, state, panel = setup
    with pytest.raises(ValueError):
        await panel.callback("fin:select:SBP", 7, state)
    api.item["user"]["id"] = "another seller"
    with pytest.raises(ValueError):
        await panel.callback("fin:cost:item", 7, state)
    assert store.setting("finance:7:cost:item") is None


async def test_wizard_financial_choices_cost_and_snapshot_survive_restart(setup):
    store, api, products, state, _ = setup
    wizard = Wizard(store, api, products)
    ident = store.draft(
        7,
        {
            "step": "preview",
            "name": "Test product",
            "description": "Description fixture",
            "price": 2000,
            "fields": {},
            "photos": [],
            "delivery": "manual",
            "category_id": "category",
            "obtaining_type_id": None,
            "fee_multiplier": "0.1",
            "fee_checked_at": now(),
        },
    )
    await state.set_state("wizard")
    await state.set_data({"draft_id": ident, "revision": 0})
    await wizard.screen(7, state)

    async def click(action):
        nonce = (await state.get_data())["view_token"]
        return await wizard.callback(7, state, f"w:{nonce}:{action}")

    await click("finance_open")
    await click("finance_method")
    await click("choose:0")  # LOCAL was filtered, first option is actual SBP
    await click("finance_cost")
    state = FSMContext(SQLiteStorage(store), state.key)
    await wizard.message(SimpleNamespace(text="500", from_user=SimpleNamespace(id=7)), state)
    text, _ = await click("finance_done")
    assert "1692.00" in text and "500.00" in text
    assert "Прибыль:" not in text  # no confirmed free placement quote yet
    draft = store.get_draft(ident, 7)["payload"]
    assert draft["withdrawal"]["provider"]["id"] == "SBP" and draft["cost"] == "500.00"
    await click("finance_open")
    await click("finance_refresh")
    assert not store.rows("SELECT * FROM outbox")


def test_financial_quote_whitelist_excludes_buyer_secrets(setup):
    store, _, _, _, _ = setup
    save_quote(
        store,
        7,
        "draft",
        {
            "price": 1000,
            "fields": {"password": "private buyer credential"},
            "delivery_data": "private buyer credential",
        },
        calculate(1000),
    )
    quote = store.rows("SELECT payload FROM financial_quotes")[0]["payload"]
    assert "private buyer credential" not in quote
    assert "ESTIMATE_ONLY" in quote


def test_existing_database_migration_is_repeatable_and_preserves_settings(tmp_path):
    path = tmp_path / "migration.sqlite"
    store = Store(path)
    store.set_setting("keep", 123)
    store.execute("DROP TABLE financial_quotes")
    store.execute("DELETE FROM schema_migrations WHERE version=2")
    store.close()
    store = Store(path)
    try:
        assert store.setting("keep") == 123
        assert store.rows("SELECT version FROM schema_migrations WHERE version=2")
        store.apply_migrations()
        assert store.rows("SELECT * FROM financial_quotes") == []
    finally:
        store.close()


async def test_premium_preview_is_one_time_quote_not_a_purchase_or_repeated_expense(setup):
    store, api, _, state, panel = setup
    api.item.update(status="DRAFT", price=1600, raw_price=2000)
    await panel.callback("fin:placement:item", 7, state)
    nonce = (await state.get_data())["placement_token"]
    text, _ = await panel.callback(f"fin:place:{nonce}:1", 7, state)
    assert "37.00" in text
    assert not store.rows("SELECT * FROM outbox")
    assert not store.rows("SELECT * FROM financial_operations")
    for _ in range(2):
        await panel.callback("finance:item", 7, state)
    quotes = store.rows("SELECT payload FROM financial_quotes ORDER BY created_at")
    assert all(json.loads(q["payload"])["result"]["placement_expense"] is None for q in quotes)
    assert all(json.loads(q["payload"])["inputs"]["placement_quote"]["price"] == "37.00" for q in quotes)
    assert json.loads(quotes[-1]["payload"])["inputs"]["placement_quote"]["base_price"] == 2000
    api.item["status"] = "SOLD"
    with pytest.raises(ValueError):
        await panel.callback("fin:placement:item", 7, state)


async def test_saved_product_cost_can_be_set_during_playerok_outage(shop):
    from app.playerok.adapter import APIError
    from tests.test_universal_delivery_e2e import click, setup_ui

    r = await shop()
    await setup_ui(r, "unique_stock")
    await r.ui.click("item:lot")
    original = r.api.call

    async def offline(method, **kwargs):
        raise APIError("CONNECTION_NOT_VERIFIED")

    r.api.call = offline
    await r.ui.click("finance:lot")
    await click(r, action="fin:cost:lot")
    await r.ui.message("32,50")
    assert r.store.setting("finance:7:cost:lot") == "32.50"
    assert "Себестоимость сохранена" in r.session.last_panel.text
    assert not r.api.messages and not r.api.remote.payments
    r.api.call = original


from tests.test_stage_a_controls import shop as shop_fixture

shop = shop_fixture
