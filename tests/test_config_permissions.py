"""Opt-in flag compatibility and startup isolation; no production credentials or network."""

import asyncio
from itertools import product
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.config import Config
from app.database.store import Store
from app.gifts.settings import GiftSettings
from tests.test_playerok_connection import API

FLAGS = (
    "PAID_OPERATIONS_ENABLED",
    "PLAYEROK_WRITES_ENABLED",
    "PLAYEROK_AUTOMATION_ENABLED",
    "TG_GIFT_PAYMENTS_ENABLED",
    "TG_GIFT_REFUNDS_ENABLED",
    "PLAYEROK_AUTO_ACCEPT_AGREEMENTS",
)
CASES = list(product((False, True), repeat=len(FLAGS)))


def configuration(tmp_path, monkeypatch, case):
    for key in FLAGS:
        monkeypatch.delenv(key, raising=False)
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "PLAYEROK_HEADER_STRING",
        "TELEGRAM_TRANSPORT",
        "DATABASE_PATH",
        "OWNER_IDS",
        "TELEGRAM_USE_PROXY",
        "TG_USER_SESSION_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "config.env"
    path.write_text(
        "TELEGRAM_BOT_TOKEN=123:"
        + "x" * 35
        + "\nPLAYEROK_HEADER_STRING=token=test\nOWNER_IDS=7\nTELEGRAM_TRANSPORT=direct\n"
        + "\n".join(k + "=" + str(v).lower() for k, v in zip(FLAGS, case, strict=True)),
        encoding="utf-8",
    )
    return Config.load(str(path)), GiftSettings.load(str(path))


@pytest.mark.parametrize("case", CASES)
def test_legacy_paid_flag_never_blocks_or_enables_other_flags(tmp_path, monkeypatch, case):
    config, gifts = configuration(tmp_path, monkeypatch, case)
    _, writes, automatic, payment, refund, agreements = case
    assert config.writes_enabled is writes
    assert config.automation_enabled is automatic
    assert config.auto_accept_agreements is agreements
    assert gifts.payment_enabled is payment
    assert gifts.refund_enabled is refund


@pytest.mark.parametrize("case", CASES)
async def test_real_startup_worker_selection_for_every_flag_combination(tmp_path, monkeypatch, case):
    from dataclasses import replace

    from app.__main__ import run_connected

    config, gifts = configuration(tmp_path, monkeypatch, case)
    _, writes, automatic, payment, refund, agreements = case
    store = Store(tmp_path / "startup.sqlite")
    api = API()
    api.account.writes_enabled = writes
    api.gift_settings = gifts
    api.gift_sender = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(store, "backup", Mock())
    monkeypatch.setattr(store, "close", Mock())
    monkeypatch.setattr("app.__main__.Store", lambda _: store)
    monkeypatch.setattr("app.__main__.SafeAccount", lambda *a, **k: api.account)
    monkeypatch.setattr("app.__main__.Playerok", lambda _: api)
    session = SimpleNamespace(middleware=Mock(), close=AsyncMock())
    bot = SimpleNamespace(
        get_me=AsyncMock(), session=session, get_webhook_info=AsyncMock(return_value=SimpleNamespace(url=""))
    )
    monkeypatch.setattr("app.__main__.Bot", lambda *a, **k: bot)
    monkeypatch.setattr("app.__main__.AiohttpSession", lambda **k: session)
    scheduled = []

    def start(self, name, factory, stop):
        scheduled.append(name)
        return asyncio.create_task(stop.wait())

    monkeypatch.setattr("app.__main__.Supervisor.start", start)

    async def polling(*args, **kwargs):
        assert api.connection.state["status"] == "CONNECTED"
        assert api.account.writes_enabled is writes
        assert api.account.automation_enabled is automatic
        assert api.account.auto_accept_agreements is agreements
        assert api.gift_settings.payment_enabled is payment
        assert api.gift_settings.refund_enabled is refund

    monkeypatch.setattr("app.__main__.Dispatcher.start_polling", polling)
    try:
        await run_connected(replace(config, database_path=store.path, realtime_enabled=False))
        assert ("outbox" in scheduled) is (writes or agreements)
        for name in ("auto-replies", "future-order-enrollment", "delivery-chains", "telegram-gifts"):
            assert (name in scheduled) is (writes and automatic)
        assert ("agreements-resume" in scheduled) is agreements
        assert api.calls == ["get"]
        assert not store.rows("SELECT * FROM outbox")
        assert not store.rows("SELECT * FROM gift_jobs")
        api.gift_sender.close.assert_awaited_once()
    finally:
        Store.close(store)


@pytest.fixture
def gift_rig(tmp_path):
    from tests import test_gifts as shared

    yield from shared.rig.__wrapped__(tmp_path)


@pytest.mark.parametrize("invalid_recipient", [False, True])
@pytest.mark.parametrize("case", CASES)
async def test_gift_outbox_and_refund_respect_only_specific_permissions(gift_rig, case, invalid_recipient):
    from dataclasses import replace

    from telethon import errors

    from app.gifts.service import GiftService
    from app.services.orders import Orders

    r = gift_rig
    # Prepare a valid local opt-in using only mock transports, then apply the selected flags.
    await r.service.bind(7, "item", "heart")
    await r.service.arm(7, "item", r.service.listing("item")["revision"])
    _, writes, automatic, payment, refund, agreements = case
    r.api.account.writes_enabled = writes
    r.api.account.automation_enabled = automatic
    r.api.account.auto_accept_agreements = agreements
    r.api.gift_settings = replace(r.api.gift_settings, payment_enabled=payment, refund_enabled=refund)
    r.api.gift_sender.settings = r.api.gift_settings
    r.service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    if invalid_recipient:
        r.remote.resolve_error = errors.UsernameNotOccupiedError(None)
        r.api.support_refund = True
    Orders(r.store).ingest(r.api.add_order())
    allowed = writes and automatic and payment
    assert r.service.runtime() is allowed
    assert r.service.discover() == int(allowed)
    if not allowed:
        assert not await r.worker.once()
        assert not r.store.rows("SELECT * FROM outbox")
    else:
        await r.worker.once()
        job = r.service.job("order")
        if invalid_recipient:
            assert not r.remote.payments
            assert job["refund_state"] == ("QUEUED" if refund else "MANUAL")
            if refund:
                await r.worker.once(operation_id=job["refund_operation_id"])
                assert r.service.job("order")["refund_state"] == "CONFIRMED"
                assert len(r.api.refunds) == 1
                await r.worker.once(operation_id=job["refund_operation_id"])
                assert len(r.api.refunds) == 1
        else:
            assert len(r.remote.payments) == 1
            assert job["state"] == "SUCCEEDED"
            assert job["refund_state"] == "NONE"
        assert not r.store.rows(
            "SELECT * FROM outbox WHERE kind IN ('create','edit','delete','publish','delivery','fulfill','reply')"
        )
    assert not r.api.sent
    if not (allowed and invalid_recipient and refund):
        assert not r.api.refunds
    if not allowed or invalid_recipient:
        assert not r.remote.payments


@pytest.mark.parametrize("flag", ["payment", "writes", "automation"])
async def test_gift_arm_explains_exact_disabled_permission(gift_rig, flag):
    from dataclasses import replace

    from app.gifts.core import GiftValidationError
    from app.gifts.service import GiftService

    r = gift_rig
    expected = {
        "payment": "TG_GIFT_PAYMENTS_ENABLED",
        "writes": "PLAYEROK_WRITES_ENABLED",
        "automation": "PLAYEROK_AUTOMATION_ENABLED",
    }
    if flag == "payment":
        r.api.gift_settings = replace(r.api.gift_settings, payment_enabled=False)
    else:
        setattr(r.api.account, "writes_enabled" if flag == "writes" else "automation_enabled", False)
    service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    with pytest.raises(GiftValidationError, match=expected[flag]):
        await service.arm(7, "item", 0)
    assert not r.remote.payments and not r.api.refunds and not r.api.sent


def test_supported_write_denial_has_specific_owner_diagnostic():
    from app.playerok.adapter import APIError
    from app.telegram.presentation import error_text

    text = error_text(APIError("WRITES_DISABLED"))
    assert "выключены" in text and "профиле" in text
    assert "сборке" not in text
