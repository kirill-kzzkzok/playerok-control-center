"""Real UI/services; only external API boundaries are simulated."""

import pytest

from app.database.store import now
from app.gifts.mtproto import GiftFailure
from app.playerok.adapter import APIError
from app.services.product_readiness import ProductReadiness
from tests.test_playerok_delivery import configure
from tests.test_telegram_transport import production_dispatcher
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_universal_delivery_e2e import simulation as simulation_fixture
from tests.test_ux_redesign import buttons


@pytest.fixture
async def shop(tmp_path):
    async for create in simulation_fixture.__wrapped__(tmp_path):
        yield create


async def test_gift_shortage_and_stale_balance_have_actions_and_no_writes(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    r.store.set_setting("gifts:balance", {"stars": 1, "at": "2020-01-01T00:00:00Z"})
    r.store.set_setting("gifts:listing_options:lot", {})
    snapshot = ProductReadiness(r.store, r.api, r.api.gift_sender)
    result = snapshot.snapshot("lot", item=r.api.item)
    codes = {p["code"] for p in result["problems"]}
    assert {"BALANCE_STALE", "STARS_SHORTAGE"} <= codes
    assert result["shortage"] == 14
    assert result["message"] == "Стандартное уведомление после подтверждённой доставки"
    assert not result["ready"]
    assert not r.api.remote.payments


async def test_one_blocked_order_does_not_stop_an_independent_sale(shop):
    from app.services.orders import Orders
    from app.workers.delivery_chains import ChainWorker
    from app.workers.enrollment import EnrollmentWorker

    r = await shop()
    await setup_ui(r, "fixed_text", "confirmed-delivery")
    for ident in ("bad", "good"):
        Orders(r.store).ingest(r.api.order(ident))
    await EnrollmentWorker(r.store, r.api).once()
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_chat" and kwargs["chat_id"] == "chat-bad":
            return {"id": "chat-bad", "is_texting_allowed": False}
        return await original(method, **kwargs)

    r.api.call = call
    for _ in range(8):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert r.store.rows("SELECT status FROM delivery_chains WHERE order_id='bad'") == [{"status": "BLOCKED"}]
    assert r.store.rows("SELECT status FROM delivery_chains WHERE order_id='good'") == [
        {"status": "SUCCEEDED"}
    ]
    result = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
    problem = next(p for p in result["problems"] if p["code"] == "CHAIN_BLOCKED")
    assert not problem["blocking"]
    assert result["state"] != "STOPPED"
    await r.ui.click("item:lot")
    assert "Остановлено — требуется решение" not in r.session.last_panel.text


@pytest.mark.parametrize("reply,fulfill", [(True, False), (False, True), (True, True), (False, False)])
async def test_platform_optional_actions_do_not_create_false_problems(shop, reply, fulfill):
    r = await shop()
    await configure(r, reply=reply, fulfill=fulfill)
    result = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
    assert result["mode"] == "playerok"
    assert result["enabled"] == (reply or fulfill)
    assert not any(p["code"] in {"TEXT_MISSING", "STOCK_EMPTY"} for p in result["problems"])
    if not reply and not fulfill:
        assert result["state"] == "OFF"


async def test_status_opens_same_card_and_saved_diagnostic_callback_is_readonly(shop):
    r = await shop()
    await configure(r)
    mutations = len(r.api.mutations)
    await r.ui.message("/status")
    assert set(buttons(r.session.last_panel)) == {"status:close"}
    await r.ui.click("item:lot")
    assert "iv:delivery:lot" in buttons(r.session.last_panel)
    await r.ui.click("rd:check:lot")  # Compatibility with an already-sent diagnostic button.
    assert "Проверка готовности" in r.session.last_panel.text
    back = next(key for key in buttons(r.session.last_panel) if key.startswith("nav:back:"))
    await click(r, action=back)
    assert "iv:delivery:lot" in buttons(r.session.last_panel)
    assert len(r.api.mutations) == mutations and not r.api.messages and not r.api.remote.payments


@pytest.mark.parametrize(
    "fault,code",
    [
        (None, None),
        ("balance", "STARS_SHORTAGE"),
        ("stale", "BALANCE_STALE"),
        ("telegram", "MTPROTO"),
        ("limit", "STARS_LIMIT"),
        ("catalog", "GIFT_UNAVAILABLE"),
        ("sync", "SYNC_ERROR"),
        ("binding", "BINDING"),
        ("description", "ITEM_CHANGED"),
        ("pending", "IN_PROGRESS"),
        ("uncertain", "UNCERTAIN"),
    ],
)
async def test_gift_reasons_and_actions_are_structured(shop, fault, code):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    if fault == "balance":
        r.store.set_setting("gifts:balance", {"stars": 0, "at": now()})
    elif fault == "stale":
        r.store.set_setting("gifts:balance", {"stars": 100, "at": "2020-01-01T00:00:00Z"})
    elif fault == "telegram":
        r.store.set_setting("gifts:connection", {"status": "ERROR", "at": now()})
    elif fault == "limit":
        r.store.set_setting("gifts:limits", {"order": 1, "day": 1})
    elif fault == "catalog":
        r.store.execute("UPDATE gift_catalog SET available=0 WHERE code='heart'")
    elif fault == "sync":
        r.store.set_setting("sync:health", {"status": "ERROR"})
    elif fault == "binding":
        r.store.execute("UPDATE logical_products SET state='PENDING'")
    elif fault == "description":
        r.api.item["description"] += " edited"
    elif fault in {"pending", "uncertain"}:
        # A genuine persisted description operation is used as fault input.
        r.store.execute("UPDATE outbox SET status=?", ("UNCERTAIN" if fault == "uncertain" else "PENDING",))
    result = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
    if code:
        assert code in {p["code"] for p in result["problems"]}
        assert not result["ready"] or fault == "pending"  # Known queued work is not an unknown outcome.
    else:
        assert result["ready"], result["problems"]
    for problem in result["problems"]:
        assert problem["text"] and problem["action"][0] and len(problem["action"][1].encode()) <= 64
    assert not r.api.messages and not r.api.remote.payments


@pytest.mark.parametrize("handler,value", [("unique_stock", None), ("fixed_text", "secret inventory")])
async def test_every_mode_navigation_refresh_restart_and_no_duplicate_side_effects(shop, handler, value):
    r = await shop()
    await setup_ui(r, handler, value)
    before = r.store.rows("SELECT id,status FROM outbox")
    for route in (
        "item:lot",
        "rd:check:lot",
        "item:lot",
        "iv:messages:lot",
        "item:lot",
        "iv:completion:lot",
        "item:lot",
        "iv:history:lot",
        "item:lot",
    ):
        await r.ui.click(route)
        assert not r.session.last_panel.text.startswith("❗")
    r.ui.dp = production_dispatcher(r.store, r.api)
    await r.ui.message("/status")
    assert set(buttons(r.session.last_panel)) == {"status:close"}
    await r.ui.click("item:lot")
    assert value is None or value not in r.session.last_panel.text
    assert r.store.rows("SELECT id,status FROM outbox") == before
    assert not r.api.messages and not r.api.remote.payments
    assert not r.store.rows("SELECT * FROM reservations")
    if handler == "unique_stock":
        result = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
        assert "STOCK_EMPTY" not in {p["code"] for p in result["problems"]}
        r.store.execute("UPDATE inventory SET status='DELIVERED'")
        assert "STOCK_EMPTY" in {
            p["code"] for p in ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)["problems"]
        }


async def test_network_failure_keeps_last_success_and_does_not_mask_internal_error(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    service = ProductReadiness(r.store, r.api)
    good = await service.check(7, "lot")
    assert good["ready"]
    r.store.set_setting("readiness:lot", {"success": good["checked_at"]})

    async def failed_balance():
        raise GiftFailure("MTPROTO_TIMEOUT")

    r.api.gift_sender.balance = failed_balance
    result = await service.check(7, "lot")
    assert not result["ready"] and result["checked_at"] == good["checked_at"]
    assert "CHECK_FAILED" in {p["code"] for p in result["problems"]}
    r.store.set_setting("readiness:lot", {})

    async def broken_call(*args, **kwargs):
        raise RuntimeError("internal defect")

    r.api.call = broken_call
    with pytest.raises(RuntimeError):
        await service.check(7, "lot")
    assert not service.snapshot("lot", item=r.api.item)["ready"]
    assert not r.api.remote.payments


async def test_api_failure_and_rapid_rechecks_are_read_only(shop):
    r = await shop()
    await configure(r)
    original = r.api.call
    calls = []

    async def unavailable(method, **kwargs):
        calls.append(method)
        if method == "get_item":
            raise APIError("NETWORK_ERROR")
        return await original(method, **kwargs)

    r.api.call = unavailable
    before = r.store.rows("SELECT id,status FROM outbox")
    await r.ui.click("rd:check:lot")
    assert "Проверка не завершена" in r.session.last_panel.text
    await r.ui.click("rd:check:lot")
    assert calls == ["get_item"]
    assert r.store.rows("SELECT id,status FROM outbox") == before
    assert not r.api.messages and not r.api.remote.payments


async def test_repeated_status_sends_new_reports_without_editing_primary_card(shop):
    r = await shop()
    await configure(r)
    panel = r.store.setting("navigation:123:7")
    before = len(r.session.calls)
    for _ in range(3):
        await r.ui.message("/status")
    calls = r.session.calls[before:]
    assert sum(c.__api_method__ == "sendMessage" for c in calls) == 3
    assert sum(c.__api_method__ == "deleteMessage" for c in calls) == 3
    assert not any(c.__api_method__ == "editMessageText" for c in calls)
    assert r.store.setting("navigation:123:7") == panel


async def test_partial_catalog_card_is_unknown_not_changed_description(shop):
    r = await shop()
    await configure(r)
    partial = {key: r.api.item[key] for key in ("id", "name", "price", "status", "user")}
    result = ProductReadiness(r.store, r.api).snapshot("lot", item=partial)
    codes = {p["code"] for p in result["problems"]}
    assert "ITEM_CHANGED" not in codes
    assert "ITEM_UNCHECKED" in codes and result["state"] == "UNKNOWN"


async def test_readiness_check_remains_readonly_and_reachable_after_lost_ack(shop):
    from aiogram.exceptions import TelegramNetworkError

    r = await shop()
    await configure(r)
    r.ui.dp = production_dispatcher(r.store, r.api)
    await r.ui.message("/status")
    original = r.session.make_request

    async def no_ack(bot, method, timeout=None):
        if method.__api_method__ == "answerCallbackQuery":
            raise TelegramNetworkError(method=method, message="lost ACK")
        return await original(bot, method, timeout)

    r.session.make_request = no_ack
    before = r.store.rows("SELECT id,status FROM outbox")
    await r.ui.click("rd:check:lot")
    assert "Проверка готовности" in r.session.last_panel.text
    assert r.store.rows("SELECT id,status FROM outbox") == before
    assert not r.api.messages and not r.api.remote.payments
