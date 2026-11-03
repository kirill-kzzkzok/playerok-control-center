"""Reproducible offline latency profile, not a real proxy/SLA measurement."""

import asyncio
import json
from time import perf_counter

import pytest

from tests.test_fix_observability import case as case_fixture


@pytest.fixture(name="case")
async def _case(tmp_path):
    async for value in case_fixture.__wrapped__(tmp_path):
        yield value


@pytest.mark.parametrize("action", ["items", "item:lot", "compose:chat"])
async def test_offline_ack_and_read_profile_does_not_drop_safe_action(case, monkeypatch, action):
    r = case
    original_request, original_api = r.session.make_request, r.api.call
    monkeypatch.setattr("app.telegram.callback_ack.ACK_TIMEOUT", 0.02)

    async def slow_ack(bot, method, timeout=None):
        if method.__api_method__ == "answerCallbackQuery":
            await asyncio.sleep(0.04)
        return await original_request(bot, method, timeout)

    async def slow_read(method, **kwargs):
        await asyncio.sleep(0.03)
        return await original_api(method, **kwargs)

    r.session.make_request, r.api.call = slow_ack, slow_read
    source = r.store.setting("navigation:123:7")["id"]
    message_ids = set(r.session.messages)
    begin = perf_counter()
    await r.ui.click(action)
    elapsed = perf_counter() - begin
    if action.startswith("compose:"):
        assert r.store.rows("SELECT state FROM fsm")[0]["state"] == "chatcard:reply"
        # The reply form now belongs to the clicked navigation message, not a
        # newly created notification card. ACK timeout still permits this safe
        # local form; it never authorizes sending the reply.
        assert r.store.setting("navigation:123:7")["id"] == source
        assert "Напишите ответ покупателю" in r.session.messages[source]["text"]
        assert set(r.session.messages) == message_ids
    else:
        assert r.store.setting("navigation:123:7")["route"] == action
    assert not r.store.rows("SELECT * FROM outbox")
    print(
        json.dumps(
            {
                "profile": "OFFLINE_CONTROLLED_BOUNDARY",
                "action": action,
                "elapsed_seconds": round(elapsed, 6),
                "ack_timeout_seconds": 0.02,
                "read_delay_seconds": 0.03,
                "completed": True,
            }
        )
    )
