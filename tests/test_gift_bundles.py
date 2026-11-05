import asyncio

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from telethon import errors, functions, types

from app.database.store import Store
from app.gifts.core import SEED_GIFTS, GiftValidationError, gift_code_from_description
from app.gifts.mtproto import PersonalGiftSender
from app.gifts.service import GiftService
from app.gifts.worker import GiftWorker
from app.services.orders import Orders
from app.services.products import Products
from app.telegram.storage import SQLiteStorage
from app.workers.outbox import Executor, OutboxWorker
from tests import test_gifts as shared
from tests.test_gifts import MTProtoTransport


@pytest.fixture
def rig(tmp_path):
    yield from shared.rig.__wrapped__(tmp_path)


from tests.test_telegram_integration import Interaction, TelegramSession, dispatcher


class BundleTransport(MTProtoTransport):
    def __init__(self):
        super().__init__()
        self.catalog = {
            code: types.StarGift(ident, types.DocumentEmpty(1), price, price - 2)
            for code, (_, ident, price) in SEED_GIFTS.items()
        }
        self.after_send = None
        self.fail_at = None

    async def __call__(self, request):
        if isinstance(request, functions.payments.GetStarGiftsRequest):
            self.calls.append(request)
            return types.payments.StarGifts(0, list(self.catalog.values()), [], [])
        if isinstance(request, functions.payments.GetPaymentFormRequest):
            self.form_price = next(g.stars for g in self.catalog.values() if g.id == request.invoice.gift_id)
        if isinstance(request, functions.payments.SendStarsFormRequest):
            self.gift = next(g for g in self.catalog.values() if g.id == request.invoice.gift_id)
            if self.fail_at == len(self.payments):
                self.pay_error = TimeoutError()
            result = await super().__call__(request)
            if self.after_send:
                self.after_send()
            return result
        return await super().__call__(request)


async def setup(r, codes="bear, bear, rose", *, auto=False):
    transport = BundleTransport()
    r.api.gift_sender = PersonalGiftSender(r.api.gift_settings, client=transport)
    r.service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    r.remote = transport
    r.api.item["description"] = "Real bundle\ngift: " + codes
    await r.service.bind(7, "item", codes)
    await r.service.arm(7, "item", r.service.listing("item")["revision"], auto_fulfill=auto)
    Orders(r.store).ingest(r.api.add_order())
    assert r.service.discover() == 1
    return transport


@pytest.mark.parametrize(
    "codes", ["heart", "bear, bear", "bear, rose", "heart, bear, rose", "rose, rose, rose"]
)
async def test_ordered_bundle_all_receipts_once_notice_and_fulfillment(rig, codes):
    r = rig
    t = await setup(r, codes, auto=True)
    expected = codes.split(", ")
    for i, code in enumerate(expected):
        await r.worker.once()
        steps = r.service.steps("order")
        assert [s["state"] for s in steps[: i + 1]] == ["SUCCEEDED"] * (i + 1)
        assert [s["code"] for s in steps] == expected
        assert len(t.payments) == i + 1
        assert t.payments[i].invoice.gift_id == SEED_GIFTS[code][1]
        if i < len(expected) - 1:
            assert r.service.job("order")["state"] == "PENDING" and not r.api.sent
            await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
            assert not r.store.rows("SELECT 1 FROM outbox WHERE kind IN ('gift_notice','fulfill')")
    assert len({s["operation_id"] for s in steps}) == len(expected)
    assert len({s["reference"] for s in steps}) == len(expected)
    assert r.service.job("order")["stars"] == sum(SEED_GIFTS[c][2] for c in expected)
    w = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    await w.once()
    await r.worker.once()
    await r.worker.once()
    await w.once()
    assert len(r.api.sent) == 1 and "Все предусмотренные" in r.api.sent[0][1]
    assert r.api.orders["order"]["status"] == "SENT"
    assert r.service.discover() == 0


@pytest.mark.parametrize("fault", ["total-budget", "total-balance", "second-price", "second-unavailable"])
async def test_whole_package_preflight_before_first_payment(rig, fault):
    r = rig
    t = await setup(r, "bear, rose")
    if fault == "total-budget":
        r.service.set_limits(7, 30, 30)
    if fault == "total-balance":
        t.stars = 30
    if fault == "second-price":
        t.catalog["rose"].stars = 26
    if fault == "second-unavailable":
        del t.catalog["rose"]
    await r.worker.once()
    assert not t.payments and not r.api.refunds
    # Resource shortages retry free preflight; price and budget authority remain blocked.
    assert r.service.job("order")["state"] == (
        "PENDING" if fault in {"total-balance", "second-unavailable"} else "BLOCKED"
    )


@pytest.mark.parametrize(
    "fault", ["balance", "unavailable", "stop", "cancel", "description", "recipient", "invalid-username"]
)
async def test_partial_delivery_blocks_later_steps_no_full_refund_or_sent(rig, fault):
    r = rig
    t = await setup(r, "bear, rose, heart", auto=True)
    await r.worker.once()
    assert r.service.steps("order")[0]["state"] == "SUCCEEDED"
    if fault == "balance":
        t.stars = 0
    elif fault == "unavailable":
        del t.catalog["rose"]
    elif fault == "stop":
        r.service.stop(7)
    elif fault == "cancel":
        r.api.orders["order"]["status"] = "ROLLED_BACK"
    elif fault == "description":
        r.api.item["description"] = "Changed\ngift: heart"
    elif fault == "recipient":
        r.api.orders["order"]["obtaining_fields"][0]["value"] = "@Otheruser"
    elif fault == "invalid-username":
        t.resolve_error = errors.UsernameNotOccupiedError(None)
    await r.worker.once()
    assert len(t.payments) == 1 and not r.api.refunds
    steps = r.service.steps("order")
    assert (
        steps[0]["state"] == "SUCCEEDED" and steps[1]["state"] == "BLOCKED" and steps[2]["state"] == "PENDING"
    )
    assert r.service.job("order")["state"] == "BLOCKED"
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind IN ('fulfill','gift_refund','gift_notice')")


@pytest.mark.parametrize("lost_position", [0, 1])
async def test_lost_ack_without_message_id_blocks_remaining_and_never_guesses(rig, lost_position):
    r = rig
    t = await setup(r, "bear, rose, heart")
    t.fail_at = lost_position
    for _ in range(lost_position + 1):
        await r.worker.once()
    assert r.service.job("order")["state"] == "UNCERTAIN"
    assert await r.worker.once() is False
    assert len(t.payments) == lost_position + 1
    t.pay_error = None
    t.fail_at = None
    from app.gifts.mtproto import GiftFailure

    with pytest.raises(GiftFailure, match="DELIVERY_CORRELATION_UNKNOWN"):
        await r.service.reconcile(7, "order")
    assert r.service.steps("order")[lost_position]["state"] == "UNCERTAIN"
    assert not r.store.rows("SELECT 1 FROM outbox WHERE kind='gift_notice'")
    assert await r.worker.once() is False
    assert len(t.payments) == lost_position + 1
    assert not r.api.refunds


async def test_restart_between_steps_and_after_partial_block(rig):
    r = rig
    t = await setup(r, "bear, rose")
    await r.worker.once()
    r.store.close()
    r.store = Store(r.store.path)
    r.service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    r.worker = OutboxWorker(r.store, Executor(r.store, r.api, Products(r.store, r.api)))
    try:
        assert r.store.recover() == 0
        t.stars = 0
        await r.worker.once()
        assert r.service.job("order")["state"] == "BLOCKED"
        r.store.close()
        r.store = Store(r.store.path)
        r.service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
        r.worker = OutboxWorker(r.store, Executor(r.store, r.api, Products(r.store, r.api)))
        t.stars = 100
        r.service.retry_preflight(7, "order")
        await r.worker.once()
        assert r.service.job("order")["state"] == "SUCCEEDED" and len(t.payments) == 2
    finally:
        r.store.close()


async def test_duplicate_paid_and_concurrent_package_reservations(rig):
    r = rig
    t = await setup(r, "bear, rose")
    Orders(r.store).ingest(r.api.add_order("second"))
    assert r.service.discover() == 1
    r.service.set_limits(7, 50, 65)
    await asyncio.gather(r.worker.once(), r.worker.once())
    assert len(t.payments) == 1
    # Concurrent claim order is not FIFO when enqueue timestamps are equal.
    # Either order may reserve the package; the other must be blocked.
    jobs = [r.service.job(ident) for ident in ("order", "second")]
    assert sorted(job["state"] for job in jobs) == ["BLOCKED", "PENDING"]
    winner = next(job for job in jobs if job["state"] == "PENDING")
    assert winner["reserved_stars"] == 25
    assert sum(job["reserved_stars"] for job in jobs) == 25
    await r.worker.once()
    assert len(t.payments) == 2 and r.service.job(winner["order_id"])["state"] == "SUCCEEDED"
    for ident in ["order", "second"]:
        Orders(r.store).ingest(r.api.orders[ident])
    assert r.service.discover() == 0


async def test_bundle_dispatcher_preview_restart_progress_and_partial_retry(rig):
    r = rig
    t = BundleTransport()
    r.api.gift_sender = PersonalGiftSender(r.api.gift_settings, client=t)
    r.api.item["description"] = "Real bundle\ngift: bear, bear, rose"
    r.service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    await r.service.refresh()
    await r.service.bind(7, "item", "bear, bear, rose")  # Existing legacy bundle compatibility.
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(r.store, r.api))
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.click("gft:bind:item")
        assert "Медведь ×2 — 30 Stars" in session.calls[-1].text and "55 Stars" in session.calls[-1].text
        p = (await state.get_data())["gift_bind"]
        ui.dp = dispatcher(r.store, r.api)
        await ui.click("gft:confirmbind:" + p["nonce"])
        await ui.click("gft:enable:item")
        p = (await state.get_data())["gift_arm"]
        await ui.click(f"gft:arm:item:{p['nonce']}:0")
        Orders(r.store).ingest(r.api.add_order())
        r.service.discover()
        await r.worker.once()
        t.stars = 0
        await r.worker.once()
        await ui.click("gft:job:order")
        assert "1/3" in session.calls[-1].text and "Частичная выдача" in session.calls[-1].text
        assert "Realbuyer" not in session.calls[-1].text
        t.stars = 100
        await ui.click("gft:retrycheck:order")
        p = (await state.get_data())["gift_retry"]
        ui.dp = dispatcher(r.store, r.api)
        await ui.click("gft:confirmretry:order:" + p["nonce"])
        await r.worker.once()
        await r.worker.once()
        assert len(t.payments) == 3
    finally:
        await bot.session.close()


async def test_configured_count_limit_blocks_inspection(rig):
    from dataclasses import replace

    r = rig
    r.api.item["description"] = "Item\ngift: bear, bear, rose"
    r.service.settings = replace(r.api.gift_settings, max_gifts=2)
    with pytest.raises(GiftValidationError):
        await r.service.inspect_item(7, "item")
    assert not r.store.rows("SELECT * FROM gift_listings")


def test_single_gift_migration_preserves_completed_unknown_and_pending(tmp_path):
    import json
    import sqlite3
    from contextlib import closing
    from pathlib import Path

    path = tmp_path / "legacy.sqlite"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(Path("app/database/schema.sql").read_text(encoding="utf-8"))
        db.execute("INSERT INTO schema_migrations VALUES(1,'2026-10-30')")
        for p in sorted(Path("app/database/migrations").glob("*.sql")):
            version = int(p.name.split("_", 1)[0])
            if version > 21:
                continue
            db.executescript(p.read_text(encoding="utf-8"))
            db.execute("INSERT INTO schema_migrations VALUES(?,'2026-10-30')", (version,))
        db.execute("INSERT INTO administrators VALUES(7)")
        db.execute(
            "INSERT INTO gift_listings(item_id,owner_id,gift_code,gift_id,approved_stars,description_sha256,category_id,obtaining_type_id,username_field_id,enabled) VALUES('item',7,'bear','5170233102089322756',15,'hash','stars','gift','field',1)"
        )
        for ident, state, outbox in [
            ("old-done", "SUCCEEDED", "SUCCEEDED"),
            ("old-unknown", "UNCERTAIN", "UNCERTAIN"),
            ("old-pending", "PENDING", "PENDING"),
        ]:
            db.execute("INSERT INTO orders VALUES(?, 'item','PAID','chat','100','2026-10-30','{}')", (ident,))
            db.execute(
                "INSERT INTO outbox(id,operation_key,kind,target,payload,status,created_at) VALUES(?,?,'gift_send',?,'{}',?,'2026-10-30')",
                (ident, "gift:" + ident, ident, outbox),
            )
            db.execute(
                "INSERT INTO gift_jobs(order_id,item_id,operation_id,listing_revision,gift_code,gift_id,reference,state,created_at,updated_at,stars,form_id,spent_at,receipt) VALUES(?,'item',?,1,'bear','5170233102089322756',?,?, '2026-10-30','2026-10-30',15,?,?,?)",
                (
                    ident,
                    ident,
                    "ref-" + ident,
                    state,
                    "form-" + ident if state != "PENDING" else None,
                    "2026-10-30T00:00:00+00:00" if state != "PENDING" else None,
                    json.dumps({"message_id": 7}) if state == "SUCCEEDED" else None,
                ),
            )
        db.commit()
    store = Store(path)
    try:
        assert store.rows("PRAGMA integrity_check") == [{"integrity_check": "ok"}]
        assert not store.rows("PRAGMA foreign_key_check")
        assert store.rows("SELECT enabled FROM gift_listings")[0]["enabled"] == 1
        steps = store.rows("SELECT * FROM gift_steps ORDER BY order_id")
        assert all(g["receipt_mode"] == "REFERENCE" and g["ack_message_id"] is None for g in steps)
        assert len(steps) == 3 and {g["state"] for g in steps} == {"SUCCEEDED", "UNCERTAIN", "PENDING"}
        assert all(g["position"] == 0 and g["code"] == "bear" for g in steps)
        assert store.rows("SELECT COUNT(*) n FROM outbox WHERE status='PENDING'")[0]["n"] == 1
        assert store.recover() == 0
        assert store.rows("SELECT COUNT(*) n FROM gift_steps WHERE state='UNCERTAIN'")[0]["n"] == 1
    finally:
        store.close()


async def test_full_refund_only_before_any_payment_in_bundle(rig):
    r = rig
    t = await setup(r, "bear, rose")
    r.api.support_refund = True
    t.resolve_error = errors.UsernameNotOccupiedError(None)
    await r.worker.once()
    await r.worker.once()
    assert not t.payments and r.service.job("order")["refund_state"] == "CONFIRMED"
    assert len(r.api.refunds) == 1


async def test_owner_notice_shows_partial_delivery_without_recipient_or_false_success(rig):
    from app.telegram.operation_results import OperationResults

    r = rig
    t = await setup(r, "bear, rose")
    await r.worker.once()
    t.stars = 0
    await r.worker.once()
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    try:
        await OperationResults(r.store).notify(bot)
        output = "\n".join(c.text for c in session.calls if hasattr(c, "text"))
        assert "Realbuyer" not in output
        assert "Доставлено частично" in output and "Медведь" in output and "Роза" in output
        assert not r.api.sent
    finally:
        await bot.session.close()


async def test_stale_balance_between_concurrent_preflight_and_intent_is_rejected(rig, monkeypatch):
    r = rig
    t = await setup(r, "bear, rose")
    t.stars = 70
    Orders(r.store).ingest(r.api.add_order("second"))
    r.service.discover()
    first_action = r.store.claim(lease_seconds=180)
    waiting, release = asyncio.Event(), asyncio.Event()
    counts = {}
    original = GiftService.fresh_order

    async def delayed(service, job):
        counts[job["order_id"]] = counts.get(job["order_id"], 0) + 1
        if job["order_id"] == "second" and counts["second"] == 2:
            waiting.set()
            await release.wait()
        return await original(service, job)

    monkeypatch.setattr(GiftService, "fresh_order", delayed)
    task = asyncio.create_task(r.worker.once())
    await asyncio.wait_for(waiting.wait(), 3)
    result = await r.worker.executor(first_action)
    r.store.finish(first_action["id"], first_action["lease_token"], "SUCCEEDED", result=result)
    release.set()
    await task
    assert len(t.payments) == 1 and r.service.job("second")["error_code"] == "BALANCE_OBSERVATION_STALE"


async def test_stop_after_paid_rpc_keeps_confirmed_delivery_and_blocks_next(rig):
    r = rig
    t = await setup(r, "bear, rose")
    t.after_send = lambda: r.service.stop(7)
    await r.worker.once()
    assert r.service.steps("order")[0]["state"] == "SUCCEEDED"
    await r.worker.once()
    assert r.service.steps("order")[1]["state"] == "BLOCKED" and len(t.payments) == 1
    assert not r.api.refunds


async def test_repeated_gifts_check_cumulative_availability_before_payment(rig):
    r = rig
    t = await setup(r, "bear, bear")
    t.catalog["bear"].limited_per_user = True
    t.catalog["bear"].per_user_remains = 1
    await r.worker.once()
    assert not t.payments and r.service.job("order")["error_code"] == "GIFT_QUANTITY_UNAVAILABLE"


async def test_expired_paid_step_lease_blocks_whole_package_after_restart(rig):
    r = rig
    t = await setup(r, "bear, rose")
    action = r.store.claim(lease_seconds=180)
    result = await r.worker.executor(action)
    assert len(t.payments) == 1  # emulate crash after Telegram delivery, before local finish
    assert result["gift_receipt"]["message_id"] == 1
    r.store.execute("UPDATE outbox SET lease_until=0 WHERE id=?", (action["id"],))
    r.store.recover()
    assert r.service.steps("order")[0]["state"] == "UNCERTAIN"
    assert r.service.steps("order")[1]["operation_id"] is None
    assert await r.worker.once() is False
    await r.service.reconcile(7, "order")
    await r.worker.once()
    assert len(t.payments) == 2 and r.service.job("order")["state"] == "SUCCEEDED"


async def test_live_console_records_only_committed_receipt_and_masked_recipient(rig, caplog):
    import logging
    import re

    from app.security.logging import ConsoleFormatter, SecretFilter

    r = rig
    t = await setup(r, "bear, rose")
    with caplog.at_level(logging.INFO, logger="pcc.shop"):
        await r.worker.once()
        await r.worker.once()
    rendered = []
    for record in caplog.records:
        SecretFilter().filter(record)
        rendered.append(ConsoleFormatter().format(record))
    output = "\n".join(rendered)
    assert "bear 1/2 — отправка" in output and "rose 2/2 — доставка подтверждена" in output
    assert output.count("Все подарки доставлены") == 1
    assert "Realbuyer" not in output
    # A decimal access_hash must not leak; random operation UUIDs may contain
    # these three digits without representing the sensitive numeric value.
    assert not re.search(r"(?<![A-Za-z0-9])987(?![A-Za-z0-9])", output)
    assert all("access_hash" not in record.getMessage() for record in caplog.records)
    assert len(t.payments) == 2


async def test_sqlite_failure_during_receipt_commit_never_logs_false_success_or_replays(
    rig, caplog, monkeypatch
):
    import logging
    import sqlite3

    r = rig
    t = await setup(r, "bear, rose")
    enqueue = r.store.enqueue

    def fail_next(key, *args, **kwargs):
        if key == "gift:order:1":
            raise sqlite3.OperationalError("simulated disk failure")
        return enqueue(key, *args, **kwargs)

    monkeypatch.setattr(r.store, "enqueue", fail_next)
    with caplog.at_level(logging.INFO, logger="pcc.shop"), pytest.raises(sqlite3.OperationalError):
        await r.worker.once()
    assert not any("доставка подтверждена" in rec.getMessage() for rec in caplog.records)
    assert len(t.payments) == 1 and r.service.steps("order")[0]["state"] == "SPENDING"
    monkeypatch.setattr(r.store, "enqueue", enqueue)
    r.store.execute("UPDATE outbox SET lease_until=0 WHERE kind='gift_send'")
    r.store.recover()
    assert r.service.job("order")["state"] == "UNCERTAIN"
    await r.service.reconcile(7, "order")
    await r.worker.once()
    assert len(t.payments) == 2 and r.service.job("order")["state"] == "SUCCEEDED"


async def test_binding_preview_rejects_changed_individual_prices_even_same_total(rig):
    r = rig
    t = BundleTransport()
    r.api.gift_sender = PersonalGiftSender(r.api.gift_settings, client=t)
    r.api.item["description"] = "Real bundle\ngift: bear, rose"
    r.service = GiftService(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    await r.service.refresh()
    await r.service.bind(7, "item", "bear, rose")
    original = r.service.listing("item")
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    ui = Interaction(bot, dispatcher(r.store, r.api))
    state = FSMContext(SQLiteStorage(r.store), StorageKey(bot_id=123, chat_id=7, user_id=7))
    try:
        await ui.click("gft:bind:item")
        pending = (await state.get_data())["gift_bind"]
        t.catalog["bear"].stars = 20
        t.catalog["rose"].stars = 20
        await ui.click("gft:confirmbind:" + pending["nonce"])
        assert r.service.listing("item") == original
        assert "Цена отдельного подарка изменилась" in session.calls[-1].text
    finally:
        await bot.session.close()


def test_repeated_codes_are_quantity():
    assert gift_code_from_description("Product\ngift: Bear, bear, ROSE") == "bear, bear, rose"


@pytest.mark.parametrize(
    "tag", ["bear,,rose", "bear;", ",bear", "bear,", "bear rose", "unknown", ",".join(["bear"] * 6)]
)
def test_bad_bundle_tags(tag):
    with pytest.raises(GiftValidationError):
        gift_code_from_description("Product\ngift: " + tag)
