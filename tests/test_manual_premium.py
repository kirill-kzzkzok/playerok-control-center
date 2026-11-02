"""Explicit one-off budget is independent from automatic promotion policy."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiogram import Bot

from app.database.store import Store
from app.services.paid_promotions import PaidPromotions
from app.services.products import Products
from app.workers.outbox import Executor, OutboxWorker
from tests.test_paid_promotions import PaidAPI
from tests.test_telegram_integration import Interaction, TelegramSession
from tests.test_telegram_transport import production_dispatcher
from tests.test_unified_delivery_controls import state
from tests.test_ux_redesign import buttons


@pytest.fixture
async def manual(tmp_path):
    store, api, session = Store(tmp_path / "manual.sqlite"), PaidAPI(), TelegramSession()
    store.claim_first_owner(7)
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, production_dispatcher(store, api))
    r = SimpleNamespace(
        store=store,
        api=api,
        bot=bot,
        ui=ui,
        session=session,
        worker=OutboxWorker(store, Executor(store, api, Products(store, api))),
    )
    await ui.message("/start")
    yield r
    await bot.session.close()
    store.close()


async def confirm(r, item="item"):
    await r.ui.click("payp:item:" + item)
    await r.ui.click(next(key for key in buttons(r.session.last_panel) if key.startswith("payp:pick:")))
    assert not r.api.charges
    return next(key for key in buttons(r.session.last_panel) if key.startswith("payp:commit:"))


async def test_manual_premium_with_zero_automatic_budgets_charges_once_after_confirmation(manual):
    r = manual
    confirmation = await confirm(r)
    await r.ui.click(confirmation)
    assert r.store.rows("SELECT status FROM promotion_expenses") == [{"status": "RESERVED"}]
    await asyncio.gather(r.worker.once(), r.worker.once())
    await r.ui.click(confirmation)
    assert not await r.worker.once()
    assert len(r.api.charges) == 1
    assert r.store.rows("SELECT status FROM outbox") == [{"status": "SUCCEEDED"}]
    _, policy, _ = PaidPromotions(r.store, r.api).settings.get()
    assert policy.enabled is False and policy.daily == "0"


@pytest.mark.parametrize("fault", ["lost", "changed-price", "stop", "tamper"])
async def test_manual_unknown_stop_and_authority_never_allow_blind_repeat(manual, fault):
    r = manual
    await r.ui.click(await confirm(r))
    assert r.store.rows("SELECT * FROM outbox")
    if fault == "lost":
        r.api.timeout = True
    elif fault == "changed-price":
        r.api.quotes[0]["price"] = 11
    elif fault == "stop":
        PaidPromotions(r.store, r.api).settings.stop(7)
    else:
        row = r.store.rows("SELECT id,payload FROM outbox")[0]
        payload = json.loads(row["payload"])
        payload["manual_authority"]["limit"] = "999"
        r.store.execute("UPDATE outbox SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
    await r.worker.once()
    assert len(r.api.charges) == (1 if fault == "lost" else 0)
    assert not await r.worker.once()
    assert r.store.rows("SELECT status FROM outbox") == [
        {"status": "UNCERTAIN" if fault == "lost" else "PERMANENT_FAILED"}
    ]


async def test_manual_reservations_compete_for_real_balance_without_enabling_schedule(manual):
    r = manual
    r.api.balance = 15
    await r.ui.click(await confirm(r))
    # A second real UI confirmation cannot reserve the same 15 ₽ twice.
    await r.ui.click("payp:item:other")
    await r.ui.click(next(key for key in buttons(r.session.last_panel) if key.startswith("payp:pick:")))
    assert "Недостаточно баланса" in r.session.last_panel.text
    assert len(r.store.rows("SELECT * FROM outbox")) == 1
    assert len(r.store.rows("SELECT * FROM promotion_expenses WHERE status='RESERVED'")) == 1
    assert not r.api.charges


async def test_wizard_one_off_premium_does_not_require_an_automatic_budget(manual):
    r = manual
    await r.ui.click("launch:item:item")
    pending = (await state(r).get_data())["launch"]
    await r.ui.click("ln:" + pending["nonce"] + ":quotes")
    await r.ui.click(next(key for key in buttons(r.session.last_panel) if ":quote:" in key))
    pending = (await state(r).get_data())["launch"]
    await r.ui.click("ln:" + pending["nonce"] + ":check")
    action = next(
        key for key, label in buttons(r.session.last_panel).items() if "Опубликовать и запустить" in label
    )
    await r.ui.click(action)
    await r.worker.once()
    assert len(r.api.charges) == 1
    assert r.store.rows("SELECT status FROM outbox") == [{"status": "SUCCEEDED"}]
    _, policy, _ = PaidPromotions(r.store, r.api).settings.get()
    assert policy.enabled is False and policy.daily == "0"
