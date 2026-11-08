from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from app.database.store import now
from app.services.orders import Orders
from app.services.review_thanks import THANKS
from app.workers.notifications import Notifications
from app.workers.sync import Synchronizer
from tests.test_unified_delivery_controls import shop as shop_fixture

shop = shop_fixture


@pytest.mark.parametrize(
    "rating,send", [(5, True), (4, True), (3, False), (2, False), (1, False), (None, False)]
)
async def test_review_pipeline_short_read_dedupe_and_safe_reply(shop, rating, send):
    r = await shop()
    r.store.set_setting("reviews:thanks_since", (datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    order = r.api.order("review-sale", status="CONFIRMED")
    order["user"]["username"] = "Buyer.On.Playerok"
    order["review"] = {
        "id": "review-1",
        "status": "APPROVED",
        "rating": rating,
        "text": "<прекрасно>",
        "created_at": now(),
        "creator": {"id": order["user"]["id"]},
        "user": {"id": r.api.account.id},
    }
    local = Orders(r.store)
    short = {**deepcopy(order), "review": {"id": "review-1", "rating": rating}}
    local.ingest(short)
    local.ingest(short)
    assert not r.store.rows("SELECT id FROM outbox")
    assert await Synchronizer(r.store, r.api, local).process_requests() == 1
    local.ingest(order)
    assert len(r.store.rows("SELECT id FROM outbox WHERE kind='reply'")) == int(send)
    await r.worker.once()
    await r.worker.once()
    assert [m["text"] for m in r.api.messages] == ([THANKS] if send else [])
    await Notifications(r.store, r.bot).once()
    notices = [m for m in r.session.calls if getattr(m, "text", "") and "НОВЫЙ ОТЗЫВ" in m.text]
    assert len(notices) == int(rating is not None)
    if notices:
        assert "&lt;прекрасно&gt;" in notices[0].text and "Buyer.On.Playerok" in notices[0].text
        assert 'href="https://playerok.com/deal/review-sale"' in notices[0].text
        assert notices[0].reply_markup is None
        assert f"{'⭐' * rating} {rating}/5" in notices[0].text


async def test_lost_thanks_reply_is_never_replayed(shop):
    r = await shop()
    r.store.set_setting("reviews:thanks_since", (datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    order = r.api.order("review-sale", status="CONFIRMED")
    order["review"] = {
        "id": "review-1",
        "status": "APPROVED",
        "rating": 5,
        "created_at": now(),
        "creator": {"id": order["user"]["id"]},
        "user": {"id": r.api.account.id},
    }
    Orders(r.store).ingest(order)
    from app.playerok.adapter import APIError

    send = r.api.account.send_message

    def lost(**kwargs):
        send(**kwargs)
        raise APIError("NETWORK_ERROR", uncertain=True)

    r.api.account.send_message = lost
    await r.worker.once()
    from tests.test_logical_faults import reopen

    reopen(r)
    order["review"]["text"] = "Изменённый отзыв"
    Orders(r.store).ingest(order)
    await r.worker.once()
    assert len(r.api.messages) == 1
    assert r.store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
    await Notifications(r.store, r.bot).once()
    assert not any("Неопределённый результат" in (getattr(c, "text", "") or "") for c in r.session.calls)


async def test_order_updates_use_one_card_with_link_and_no_buttons(shop):
    r = await shop()
    notice = Notifications(r.store, r.bot)
    local = Orders(r.store)
    order = r.api.order("sale")
    order["item"].update(name="🎁 СЕРДЕЧКО ОТ ДЕВУШКИ АВТОВЫДАЧА 24/7 [VPM]", price=90)
    local.ingest(order)
    await notice.once()
    for state in ("SENT", "CONFIRMED"):
        order["status"] = state
        local.ingest(order)
        await notice.once()
    from tests.test_logical_faults import reopen

    reopen(r)
    Orders(r.store).ingest(order)
    await Notifications(r.store, r.bot).once()
    updates = [
        m for m in r.session.calls if "https://playerok.com/deal/sale" in (getattr(m, "text", "") or "")
    ]
    assert [m.__api_method__ for m in updates] == ["sendMessage", "editMessageText", "editMessageText"]
    assert all(m.reply_markup is None and order["item"]["name"] in m.text for m in updates)
    assert all(m.text.split('🔗 <a href="')[0].endswith("\n\n") for m in updates)
    assert all(not m.text.split('🔗 <a href="')[0].endswith("\n\n\n") for m in updates)
    assert "Имя не указано" in updates[0].text and "+90 ₽" in updates[0].text
    assert "Покупатель подтвердил получение" in updates[-1].text


@pytest.mark.parametrize(
    "worker,code,expected",
    [
        ("sync", "CONNECTION_NOT_VERIFIED", "SUPPRESSED"),
        ("transactions", "UNAUTHORIZED_OR_CHALLENGE", "SUPPRESSED"),
        ("logical-products", "RATE_LIMITED", "SUPPRESSED"),
        ("transactions", "TRANSACTION_SCHEMA_ERROR", "SUCCEEDED"),
    ],
)
async def test_connection_recheck_alert_is_not_sent_to_owner(shop, worker, code, expected):
    from app.database.store import encode

    r = await shop()
    r.store.set_setting(
        "runtime:cycle:" + worker,
        {"consecutive_failures": 3, "incident": "temp", "error_code": code},
    )
    r.store.execute(
        "INSERT INTO inbox(key,kind,payload,received_at,processed_at) VALUES('test-sync','worker_failed',?,?,?)",
        (
            encode({"worker": worker, "incident": "temp", "error_type": code, "cycle": True}),
            now(),
            now(),
        ),
    )
    await Notifications(r.store, r.bot).once()
    assert r.store.rows("SELECT status FROM notifications WHERE event_key='test-sync'") == [
        {"status": expected}
    ]
    assert any("Фоновая обработка" in (getattr(call, "text", "") or "") for call in r.session.calls) == (
        expected == "SUCCEEDED"
    )


@pytest.mark.parametrize("mode", ["unique_stock", "telegram_gifts"])
async def test_confirmed_delivery_updates_card_and_unknown_never_announces_success(shop, mode):
    from app.gifts.worker import GiftWorker
    from app.playerok.adapter import APIError
    from app.workers.delivery_chains import ChainWorker
    from app.workers.enrollment import EnrollmentWorker
    from tests.test_universal_delivery_e2e import setup_ui

    r = await shop(gift=mode == "telegram_gifts")
    await setup_ui(r, mode, "gift: heart" if mode == "telegram_gifts" else None)
    notice = Notifications(r.store, r.bot)
    local = Orders(r.store)
    local.ingest(r.api.order("success"))
    local.ingest(r.api.order("unknown"))
    await notice.once()
    send = r.api.account.send_message

    def send_with_unknown(**kwargs):
        if kwargs["chat_id"] == "chat-unknown":
            raise APIError("NETWORK_ERROR", uncertain=True)
        return send(**kwargs)

    if mode == "unique_stock":
        r.api.account.send_message = send_with_unknown
    else:
        # The fake Telegram transport loses the second gift RPC result.
        r.api.remote.fail_at = 1

    gifts = GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender)
    for _ in range(10):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await gifts.once()
        await r.worker.once()
    await notice.once()
    await notice.once()
    delivered = [
        c.text for c in r.session.calls if (getattr(c, "text", "") or "").startswith("🎁 <b>ТОВАР ВЫДАН")
    ]
    assert len(delivered) == 1
    assert "/deal/success" in delivered[0]
    uncertain = [
        c.text
        for c in r.session.calls
        if "Неопределённый результат операции" in (getattr(c, "text", "") or "")
    ]
    assert len(uncertain) == 1 and "/deal/unknown" in uncertain[0]
    order = r.api.orders["success"]
    order["status"] = "ROLLED_BACK"
    local.ingest(order)
    await notice.once()
    refund = [c.text for c in r.session.calls if "ВОЗВРАТ ПОДТВЕРЖДЁН" in (getattr(c, "text", "") or "")]
    assert len(refund) == 1 and "/deal/success" in refund[0]
    expected = (
        "Подарок был отправлен до возврата" if mode == "telegram_gifts" else "Товар был выдан до возврата"
    )
    assert expected in refund[0]


async def test_order_buyer_name_survives_compact_same_buyer_snapshot(shop):
    r = await shop()
    order = r.api.order("buyer-name")
    order["user"]["username"] = "KnownBuyer"
    local = Orders(r.store)
    local.ingest(order)
    compact = deepcopy(order)
    compact["user"].pop("username")
    local.ingest(compact)
    assert (
        r.store.rows("SELECT json_extract(payload,'$.user.username') AS name FROM orders")[0]["name"]
        == "KnownBuyer"
    )


@pytest.mark.parametrize("status,expected", [("SENT", "SUCCEEDED"), ("ROLLED_BACK", "UNCERTAIN")])
async def test_late_server_order_status_reconciles_only_automatic_legacy_completion(shop, status, expected):
    r = await shop()
    order = r.api.order("late-completion")
    local = Orders(r.store)
    local.ingest(order)
    ident = r.store.enqueue(
        "automatic-fulfill:test",
        "fulfill",
        order["id"],
        {"mode": "legacy", "platform_followup": True, "chain_id": order["id"], "actor": 7},
    )
    claim = r.store.claim(operation_id=ident)
    r.store.mark_started(claim)
    import time

    r.store.finish(ident, claim["lease_token"], "UNCERTAIN", error="RATE_LIMITED", retry_after="60")
    request = r.store.rows("SELECT not_before FROM sync_requests WHERE key=?", ("deal:" + order["id"],))
    assert request and request[0]["not_before"] > time.time() + 55
    notices = Notifications(r.store, r.bot)
    await notices.once()
    assert not any("Неопределённый" in (getattr(call, "text", "") or "") for call in r.session.calls)
    order["status"] = status
    local.ingest(order)
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": expected}]
    await notices.once()
    assert not any("Неопределённый" in (getattr(call, "text", "") or "") for call in r.session.calls)
    assert not r.api.messages and not r.api.remote.payments
