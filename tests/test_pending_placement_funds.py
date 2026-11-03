import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from aiogram import Bot

from app.database.store import Store
from app.services.account_binding import bind_account
from app.services.products import Products
from app.workers.outbox import Executor, OutboxWorker
from tests.test_manual_premium import premium_confirmation, preview_premium
from tests.test_paid_promotions import PaidAPI
from tests.test_telegram_integration import Interaction, TelegramSession
from tests.test_telegram_transport import production_dispatcher
from tests.test_ux_redesign import buttons


class PendingAPI(PaidAPI):
    def __init__(self):
        super().__init__()
        self.balance, self.pending, self.frozen, self.blocked = 5, 53, 500, False

    async def call(self, method, **kwargs):
        value = await super().call(method, **kwargs)
        if method == "get":
            value["profile"].update(has_frozen_balance=self.blocked)
            value["profile"]["balance"].update(pending_income=self.pending, frozen=self.frozen)
        return value

    def charge(self, **kwargs):
        pending = self.balance < 10
        before = self.balance
        try:
            super().charge(**kwargs)
        finally:
            if pending:
                self.balance = before
                self.pending -= 10
                self.items[kwargs["item_id"]]["status_payment"]["provider_id"] = "PENDING_INCOME"
        return deepcopy(self.items[kwargs["item_id"]])


@pytest.fixture
async def shop(tmp_path):
    store, api, session = Store(tmp_path / "funds.sqlite"), PendingAPI(), TelegramSession()
    store.claim_first_owner(7)
    bind_account(store, api.account.id)
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, production_dispatcher(store, api))
    r = SimpleNamespace(
        store=store,
        api=api,
        bot=bot,
        session=session,
        ui=ui,
        worker=OutboxWorker(store, Executor(store, api, Products(store, api))),
    )
    await ui.message("/start")
    yield r
    await bot.session.close()
    store.close()


async def pick(r, item="item"):
    await preview_premium(r, item)


@pytest.mark.parametrize("status", ["DRAFT", "APPROVED", "SOLD"])
async def test_pending_income_pays_placement_once_after_confirmation(shop, status):
    r = shop
    r.api.items["item"].update(status=status, may_be_published=True)
    if status == "SOLD":
        await r.ui.click("relist:item:item")
        await r.ui.click(next(k for k in buttons(r.session.last_panel) if ":quote:" in k))
        prefix = "relist:"
    else:
        await pick(r)
        prefix = "ln:" if status == "DRAFT" else "payp:commit:"
    assert not r.session.last_panel.text.startswith("❗"), r.session.last_panel.text
    assert "замороз" in r.session.last_panel.text.lower()
    assert not r.api.charges
    confirmation = next(
        k
        for k in buttons(r.session.last_panel)
        if k.startswith(prefix) and (k.endswith(":commit") or prefix == "payp:commit:")
    )
    await r.ui.click(confirmation)
    await asyncio.gather(r.worker.once(), r.worker.once())
    await r.ui.click(confirmation)
    assert not await r.worker.once()
    assert len(r.api.charges) == 1 and r.api.balance == 5 and r.api.pending == 43
    assert r.store.rows("SELECT status FROM outbox") == [{"status": "SUCCEEDED"}]


@pytest.mark.parametrize("case", ["not-combined", "frozen-not-income", "blocked"])
async def test_pending_is_not_combined_or_confused_with_frozen_or_admin_block(shop, case):
    r = shop
    if case == "not-combined":
        r.api.pending = 5
    elif case == "frozen-not-income":
        r.api.pending = 0
    else:
        r.api.blocked = True
    await pick(r)
    assert not any(
        k.startswith("payp:commit:") or k.startswith("ln:") and k.endswith(":commit")
        for k in buttons(r.session.last_panel)
    )
    assert not r.store.rows("SELECT * FROM outbox") and not r.api.charges


@pytest.mark.parametrize("case", ["lost", "depleted"])
async def test_unknown_never_repeats_and_fresh_income_rechecked(shop, case):
    r = shop
    await pick(r)
    key = premium_confirmation(r)
    await r.ui.click(key)
    if case == "lost":
        r.api.timeout = True
    else:
        r.api.pending = 0
    await r.worker.once()
    assert not await r.worker.once()
    assert len(r.api.charges) == (1 if case == "lost" else 0)
    assert r.store.rows("SELECT status FROM outbox") == [
        {"status": "UNCERTAIN" if case == "lost" else "PERMANENT_FAILED"}
    ]


async def test_competing_reservations_cannot_overspend_pending_income(shop):
    r = shop
    r.api.quotes[0]["price"] = 30
    await pick(r)
    await r.ui.click(premium_confirmation(r))
    await pick(r, "other")
    assert not any(
        k.startswith("payp:commit:") or k.startswith("ln:") and k.endswith(":commit")
        for k in buttons(r.session.last_panel)
    )
    assert len(r.store.rows("SELECT * FROM promotion_expenses")) == 1
    assert not r.api.charges
