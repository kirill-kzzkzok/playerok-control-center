"""A stalled read must not block a different card's local navigation."""

import asyncio
import json
from time import perf_counter

from tests.test_fix_navigation import ui as ui_fixture

ui = ui_fixture


async def test_stalled_item_read_does_not_block_another_cards_settings(ui):
    r = ui
    a = r.store.setting("navigation:123:7")["id"]
    b = (await r.bot.send_message(7, "Independent settings")).message_id
    await r.ui.click("settings", message_id=b)
    entered, release = asyncio.Event(), asyncio.Event()
    original = r.api.call

    async def gated(method, **kwargs):
        if method == "get_item":
            entered.set()
            await release.wait()
        return await original(method, **kwargs)

    r.api.call = gated
    task = asyncio.create_task(r.ui.click("item:lot", message_id=a))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        started = perf_counter()
        await asyncio.wait_for(r.ui.click("sp:security", message_id=b), 0.8)
        elapsed = perf_counter() - started
        assert "Безопасность" in r.session.messages[b]["text"]
        assert not task.done()
        assert elapsed < 0.8
    finally:
        release.set()
        await task
    assert "&lt;Item&gt;" in r.session.messages[a]["text"]
    assert "Безопасность" in r.session.messages[b]["text"]
    assert not r.store.rows("SELECT * FROM outbox")


async def test_same_card_still_serializes_a_slow_read_and_later_navigation(ui):
    r = ui
    source = r.store.setting("navigation:123:7")["id"]
    entered, release = asyncio.Event(), asyncio.Event()
    original = r.api.call

    async def gated(method, **kwargs):
        if method == "get_item":
            entered.set()
            await release.wait()
        return await original(method, **kwargs)

    r.api.call = gated
    first = asyncio.create_task(r.ui.click("item:lot", message_id=source))
    await asyncio.wait_for(entered.wait(), 3)
    second = asyncio.create_task(r.ui.click("settings", message_id=source))
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        assert not second.done()
        assert len(r.session.acks) >= 2
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert "Настройки" in r.session.messages[source]["text"]


async def test_slow_telegram_edit_only_locks_its_own_card(ui):
    r = ui
    a = r.store.setting("navigation:123:7")["id"]
    b = (await r.bot.send_message(7, "Independent card")).message_id
    await r.ui.click("settings", message_id=b)
    entered, release = asyncio.Event(), asyncio.Event()
    original = r.session.make_request

    async def gated(bot, method, timeout=None):
        if method.__api_method__ == "editMessageText" and method.message_id == a:
            entered.set()
            await release.wait()
        return await original(bot, method, timeout)

    r.session.make_request = gated
    task = asyncio.create_task(r.ui.click("settings", message_id=a))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(r.ui.click("sp:security", message_id=b), 0.8)
        assert not task.done()
    finally:
        release.set()
        await task
    assert "Безопасность" in r.session.messages[b]["text"]


async def test_offline_card_and_local_navigation_percentiles(ui):
    r = ui
    original = r.session.make_request
    acknowledgements = []
    began = 0

    async def timed(bot, method, timeout=None):
        result = await original(bot, method, timeout)
        if method.__api_method__ == "answerCallbackQuery":
            acknowledgements.append(perf_counter() - began)
        return result

    r.session.make_request = timed
    timings = {"local": [], "card": []}
    reads = []
    for _ in range(20):
        for action, kind in (("settings", "local"), ("item:lot", "card")):
            count = len(r.api.calls)
            began = perf_counter()
            await r.ui.click(action)
            timings[kind].append(perf_counter() - began)
            if kind == "card":
                reads.append(len(r.api.calls) - count)

    def stats(values):
        ordered = sorted(values)
        return {"p50": ordered[len(ordered) // 2], "p95": ordered[int(len(ordered) * 0.95) - 1]}

    result = {kind: stats(values) for kind, values in timings.items()}
    result["ack"] = stats(acknowledgements)
    result["card_api_requests"] = {"min": min(reads), "max": max(reads)}
    result["environment"] = "OFFLINE_STATEFUL_TRANSPORT; no real network latency"
    print(json.dumps(result))
    assert result["ack"]["p95"] < 0.5
    assert result["local"]["p95"] < 1
    assert result["card"]["p95"] < 2
    assert max(reads) == 1
    assert not r.store.rows("SELECT * FROM outbox")
