import pytest

"""Cached messaging paths keep outbound preflight in the existing sender."""

from tests.test_chat_cards import cards as cards_fixture
from tests.test_chat_cards import incoming
from tests.test_ux_redesign import buttons

cards = cards_fixture


async def test_reply_form_does_not_read_playerok_before_text_input(cards):
    r = cards
    incoming(r, "1")
    await r.notifications.once()
    row = r.store.rows("SELECT * FROM chat_cards")[0]
    original = r.api.call
    reads = []

    async def call(method, **kwargs):
        reads.append(method)
        return await original(method, **kwargs)

    r.api.call = call
    await r.ui.click("cc:reply:" + row["token"], message_id=row["telegram_message_id"])
    assert not reads
    assert not r.sent and not r.store.rows("SELECT * FROM outbox")


async def test_local_chat_opens_without_playerok_and_preserves_long_text(cards):
    r = cards
    text = "first " + "длинный текст " * 90 + " END-OF-MESSAGE"
    incoming(r, "1", text=text)
    await r.notifications.once()
    reads = []

    async def unavailable(method, **kwargs):
        reads.append(method)
        raise AssertionError("view must use confirmed local messages")

    r.api.call = unavailable
    await r.ui.click("chat:chat")
    assert "END-OF-MESSAGE" in r.session.last_panel.text
    assert "Сохран" in r.session.last_panel.text
    assert not reads and not r.sent
    assert r.store.rows("SELECT kind,target FROM sync_requests") == [{"kind": "chat", "target": "chat"}]


@pytest.mark.parametrize("editing", [False, True])
async def test_readback_updates_only_current_order_panel(cards, editing):
    from app.services.orders import Orders
    from app.workers.notifications import Notifications

    r = cards
    r.api.account.id = "seller"
    order = {
        "id": "paid",
        "direction": "OUT",
        "status": "PAID",
        "has_problem": False,
        "is_automated": False,
        "item": {"id": "lot", "name": "Order product", "price": 100, "user": {"id": "seller"}},
        "chat": {"id": "chat"},
    }
    Orders(r.store).ingest(order)
    await r.ui.click("order:paid")
    panel = r.store.setting("navigation:123:7")
    ident = panel["id"]
    if editing:
        await r.ui.click("od:note:paid", message_id=ident)
    original_text = r.session.messages[ident]["text"]
    from app.workers.sync import Synchronizer

    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_deal":
            return {**order, "status": "SENT"}
        return await original(method, **kwargs)

    r.api.call = call
    await Synchronizer(r.store, r.api, Orders(r.store)).process_requests()
    await Notifications(r.store, r.bot, r.api).flush_chats()
    from app.telegram.presentation import status

    if editing:
        assert r.session.messages[ident]["text"] == original_text
    else:
        assert status("SENT", "order") in r.session.messages[ident]["text"]
    await r.ui.click("settings", message_id=ident)
    text = r.session.messages[ident]["text"]
    Orders(r.store).ingest({**order, "status": "CONFIRMED"})
    await Notifications(r.store, r.bot, r.api).flush_chats()
    assert r.session.messages[ident]["text"] == text
    assert not r.sent and not r.store.rows("SELECT * FROM outbox")


async def test_offline_reply_form_latency_samples(cards):
    import asyncio
    import json
    from time import perf_counter

    r = cards
    incoming(r, "1")
    await r.notifications.once()
    row = r.store.rows("SELECT * FROM chat_cards")[0]
    original_api = r.api.call
    original_rpc = r.session.make_request
    reads = []
    ack = []
    began = 0.0

    async def slow_api(method, **kwargs):
        reads.append(method)
        await asyncio.sleep(0.05)
        return await original_api(method, **kwargs)

    async def rpc(bot, method, timeout=None):
        result = await original_rpc(bot, method, timeout)
        if method.__api_method__ == "answerCallbackQuery":
            ack.append(perf_counter() - began)
        return result

    r.api.call = slow_api
    r.session.make_request = rpc
    samples = []
    for _ in range(20):
        began = perf_counter()
        await r.ui.click("cc:reply:" + row["token"], message_id=row["telegram_message_id"])
        samples.append(perf_counter() - began)

    def stats(values):
        values = sorted(values)
        return {
            "p50_ms": round(values[len(values) // 2] * 1000, 3),
            "p95_ms": round(values[int(len(values) * 0.95) - 1] * 1000, 3),
        }

    print(
        json.dumps(
            {
                "environment": "OFFLINE controlled transport, Playerok READ delay 50ms",
                "reply_form": stats(samples),
                "ack": stats(ack),
                "playerok_reads": len(reads),
            }
        )
    )
    assert not r.sent and not r.store.rows("SELECT * FROM outbox")


async def test_full_long_chat_text_survives_pagination_and_dispatcher_restart(cards):
    import html

    from tests.test_telegram_transport import production_dispatcher

    r = cards
    incoming(r, "1", text="<&>" * 2000 + " END")
    await r.notifications.once()
    await r.ui.click("chat:chat")
    fragments = []
    for _ in range(30):
        fragments.append(html.unescape(r.session.last_panel.text.split("\n\n", 2)[-1]))
        following = next((k for k in buttons(r.session.last_panel) if k.endswith(":textnext")), None)
        if not following:
            break
        r.ui.dp = production_dispatcher(r.store, r.api)
        await r.ui.click(following)
    body = "".join(fragments)
    assert body.count("<&>") == 2000 and body.endswith(" END")
    assert not r.sent and not r.store.rows("SELECT * FROM outbox")
