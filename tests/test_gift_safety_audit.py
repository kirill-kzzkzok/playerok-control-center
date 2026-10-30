"""Adversarial regressions: production pipeline, injected failures, no live clients."""

import asyncio
import logging

import pytest
from telethon import types

from app.database.store import now
from app.gifts.mtproto import GiftFailure
from app.gifts.service import GiftService
from app.services.account_binding import AccountBindingError, bind_account
from tests import test_gifts as shared
from tests.test_gift_bundles import setup


@pytest.fixture
def rig(tmp_path):
    yield from shared.rig.__wrapped__(tmp_path)


async def test_payer_switch_after_preflight_cannot_spend_from_unexpected_account(rig):
    r = rig
    t = await setup(r, "bear, rose")
    prepare = r.api.gift_sender.prepare

    async def changed_payer(*args):
        result = await prepare(*args)
        r.api.gift_sender.me = types.User(100, is_self=True)
        return result

    r.api.gift_sender.prepare = changed_payer
    await r.worker.once()
    assert not t.payments, "Changed personal payer reached sendStarsForm"
    assert not r.api.refunds


async def test_changed_obtaining_username_after_final_rejection_cannot_refund(rig):
    from telethon import errors

    r = rig
    t = await setup(r, "bear, rose")
    r.api.support_refund = True
    resolve = r.api.gift_sender.resolve

    async def changed_username(username):
        r.api.orders["order"]["obtaining_fields"][0]["value"] = "@Differentuser"
        t.resolve_error = errors.UsernameNotOccupiedError(None)
        return await resolve(username)

    r.api.gift_sender.resolve = changed_username
    await r.worker.once()
    await r.worker.once()
    assert not r.api.refunds, "Refund used rejection of a different receiving-field value"
    assert not t.payments


async def test_event_handler_failure_cannot_stop_or_quarantine_unattempted_gift(rig):
    r = rig
    t = await setup(r, "bear, rose")

    class BrokenHandler(logging.Handler):
        def emit(self, record):
            raise OSError("simulated console failure")

    logger = logging.getLogger("pcc.shop")
    old_level = logger.level
    handler = BrokenHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        await r.worker.once()
        await r.worker.once()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    assert len(t.payments) == 2 and r.service.job("order")["state"] == "SUCCEEDED"


def test_binding_checks_native_order_seller_identity(rig):
    r = rig
    r.store.set_setting("playerok:bound_account_id", "seller")
    r.store.execute(
        "INSERT INTO orders VALUES(?,?,?,?,?,?,?)",
        ("order", "item", "PAID", "chat", "100", now(), '{"item":{"user":{"id":"other-shop"}}}'),
    )
    with pytest.raises(AccountBindingError):
        bind_account(r.store, "seller")


async def test_personal_sender_cannot_reconnect_after_terminal_close_without_session_lock(rig):
    from app.gifts.mtproto import GiftFailure

    r = rig
    await r.api.gift_sender.connect()
    await r.api.gift_sender.close()
    with pytest.raises(GiftFailure):
        await r.api.gift_sender.connect()


@pytest.mark.parametrize("completed", [0, 1])
async def test_real_process_death_after_paid_rpc_is_uncertain_and_never_replayed(tmp_path, completed):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    from app.database.store import Store
    from app.gifts.mtproto import PersonalGiftSender
    from app.services.products import Products
    from app.workers.outbox import Executor, OutboxWorker

    script = """
import asyncio,os,json,sys
from pathlib import Path
from tests.test_gifts import rig
from tests.test_gift_bundles import setup
async def main():
    fixture=rig.__wrapped__(Path.cwd())
    r=next(fixture)
    transport=await setup(r,'bear, rose, heart')
    for i in range(int(sys.argv[1])): await r.worker.once()
    def die_after_server_acceptance():
        Path('server-witness.json').write_text(json.dumps({'payments':len(transport.payments)}))
        os._exit(33)
    transport.after_send=die_after_server_acceptance
    await r.worker.once()
asyncio.run(main())
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, str(completed)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path.cwd())},
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 33, result.stderr.decode(errors="replace")
    assert json.loads((tmp_path / "server-witness.json").read_text())["payments"] == completed + 1
    store = Store(tmp_path / "gifts.sqlite")
    api = shared.Marketplace()
    api.gift_settings = shared.GiftSettings(
        1, "dummy", "data/dummy.session", "stars", "gift", "username-field", True, 100, 100, True
    )
    transport = shared.MTProtoTransport()
    api.gift_sender = PersonalGiftSender(api.gift_settings, client=transport)
    service = GiftService(store, api, api.gift_settings, api.gift_sender)
    try:
        store.execute("UPDATE outbox SET lease_until=0 WHERE status='PROCESSING'")
        assert store.recover() == 1
        steps = service.steps("order")
        assert [s["state"] for s in steps[:completed]] == ["SUCCEEDED"] * completed
        assert steps[completed]["state"] == "UNCERTAIN"
        assert all(s["operation_id"] is None for s in steps[completed + 1 :])
        assert await OutboxWorker(store, Executor(store, api, Products(store, api))).once() is False
        with pytest.raises(GiftFailure):
            await service.reconcile(7, "order")  # fresh transport has no receipt; absence is not permission
        assert service.job("order")["state"] == "UNCERTAIN" and not transport.payments
    finally:
        store.close()


async def test_refund_server_delay_never_creates_second_refund(rig):
    from telethon import errors

    r = rig
    t = await setup(r, "bear, rose")
    r.api.support_refund = True
    t.resolve_error = errors.UsernameNotOccupiedError(None)

    def delayed_refund(deal_id, new_status):
        r.api.refunds.append(deal_id)
        return {"id": deal_id, "status": "PAID"}

    r.api.account.update_deal = delayed_refund
    await r.worker.once()
    await r.worker.once()
    assert r.service.job("order")["refund_state"] == "UNCERTAIN"
    with pytest.raises(GiftFailure):
        await r.service.reconcile_refund(7, "order")
    r.api.orders["order"]["status"] = "ROLLED_BACK"
    await r.service.reconcile_refund(7, "order")
    assert r.service.job("order")["refund_state"] == "CONFIRMED"
    assert r.api.refunds == ["order"] and not t.payments


async def test_receiver_change_after_refund_queue_is_rechecked_before_http(rig):
    from telethon import errors

    r = rig
    t = await setup(r, "bear, rose")
    r.api.support_refund = True
    t.resolve_error = errors.UsernameNotOccupiedError(None)
    await r.worker.once()
    assert r.service.job("order")["refund_state"] == "QUEUED"
    r.api.orders["order"]["obtaining_fields"][0]["value"] = "@Differentuser"
    await r.worker.once()
    assert not r.api.refunds and not t.payments
    assert r.service.job("order")["refund_state"] == "FAILED"


@pytest.mark.parametrize(
    "fault", ["other-order", "other-item", "bot", "deleted", "minimal", "unexpected-resolve", "peer-expired"]
)
async def test_unexpected_order_recipient_and_peer_fail_closed(rig, fault):
    from telethon import errors, functions

    r = rig
    t = await setup(r, "bear, rose")
    if fault == "other-order":
        r.api.orders["order"]["id"] = "other"
    elif fault == "other-item":
        r.api.orders["order"]["item"]["id"] = "other"
    elif fault == "bot":
        t.recipient.bot = True
    elif fault == "deleted":
        t.recipient.deleted = True
    elif fault == "minimal":
        t.recipient.min = True
    elif fault == "unexpected-resolve":
        original = t.__class__.__call__

        class MalformedClient:
            async def __call__(self, request):
                if isinstance(request, functions.contacts.ResolveUsernameRequest):
                    return None
                return await original(t, request)

        r.api.gift_sender.client = MalformedClient()  # sender is connected by binding already
    elif fault == "peer-expired":
        t.pay_error = errors.PeerIdInvalidError(None)
    await r.worker.once()
    assert len(t.payments) == (1 if fault == "peer-expired" else 0)
    assert r.service.job("order")["state"] == ("UNCERTAIN" if fault == "peer-expired" else "BLOCKED")
    assert not r.api.refunds


async def test_two_database_workers_cannot_claim_same_step_or_overlap_paid_dispatch(rig):
    from app.database.store import Store
    from app.services.products import Products
    from app.workers.outbox import Executor, OutboxWorker

    r = rig
    t = await setup(r, "bear, bear")
    second = Store(r.store.path)
    worker = OutboxWorker(second, Executor(second, r.api, Products(second, r.api)))
    entered, release = asyncio.Event(), asyncio.Event()
    pay = r.api.gift_sender.pay

    async def held(prepared):
        entered.set()
        await release.wait()
        return await pay(prepared)

    r.api.gift_sender.pay = held
    first = asyncio.create_task(r.worker.once())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert await worker.once() is False
        release.set()
        await first
        await worker.once()
        assert len(t.payments) == 2 and r.service.job("order")["state"] == "SUCCEEDED"
        assert len({s["operation_id"] for s in r.service.steps("order")}) == 2
    finally:
        release.set()
        await first
        second.close()


async def test_owned_mtproto_terminal_close_releases_lock_but_never_reconnects_unlocked(
    rig, tmp_path, monkeypatch
):
    from dataclasses import replace

    from app.gifts.mtproto import PersonalGiftSender
    from app.security.instance import InstanceLock

    r = rig
    client = shared.MTProtoTransport()
    settings = replace(r.api.gift_settings, session_path=str(tmp_path / "personal.session"))
    monkeypatch.setattr("app.gifts.mtproto.TelegramClient", lambda *a, **k: client)
    sender = PersonalGiftSender(settings)
    await sender.connect()
    with pytest.raises(RuntimeError), InstanceLock(settings.session_path + ".lock"):
        pass
    await sender.close()
    with InstanceLock(settings.session_path + ".lock"), pytest.raises(GiftFailure):
        await sender.connect()


async def test_close_during_connect_keeps_session_lock_until_connection_is_closed(rig, tmp_path, monkeypatch):
    from dataclasses import replace

    from app.gifts.mtproto import PersonalGiftSender
    from app.security.instance import InstanceLock

    r = rig
    client = shared.MTProtoTransport()
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_connect():
        entered.set()
        await release.wait()

    client.connect = held_connect
    settings = replace(r.api.gift_settings, session_path=str(tmp_path / "personal.session"))
    monkeypatch.setattr("app.gifts.mtproto.TelegramClient", lambda *a, **k: client)
    sender = PersonalGiftSender(settings)
    opening = asyncio.create_task(sender.connect())
    await entered.wait()
    closing = asyncio.create_task(sender.close())
    await asyncio.sleep(0)
    try:
        with pytest.raises(RuntimeError), InstanceLock(settings.session_path + ".lock"):
            pass
    finally:
        release.set()
        await opening
        await closing
