"""RED reproductions for the independent adversarial audit (2026-11-01).

Each test below captures a CONFIRMED or PROBABLE defect found by code reading.
Production code is NOT modified; these tests are expected to FAIL (RED) on the
current HEAD dcb7283 — that failure is the audit evidence.

Run: .venv/Scripts/python.exe -m pytest tests/test_audit_red_repro.py -x -q
"""

import json

import pytest

from app.database.store import Store
from app.services.orders import Orders
from app.services.replies import Replies
from app.telegram.message_notice import message_notice
from app.telegram.operation_results import OperationResults


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "audit.db"))
    s.set_setting("notifications:initialized", True)
    yield s
    s.close()


@pytest.fixture()
def admin(store):
    store.execute("INSERT INTO administrators(user_id) VALUES(?)", (700,))
    return 700


def _system_message(message_id, text, chat_id="ch1"):
    """A Playerok system chat event exactly as parsed by playerokapi:
    literal template text, user is None."""
    return {
        "id": message_id,
        "text": text,
        "created_at": "2026-11-01T10:00:00",
        "user": None,
        "chat_id": chat_id,
    }


# ----------------------------------------------------------------------------
# DEFECT A (P1): system events (ITEM_PAID etc.) become owner notifications
# "Неизвестный отправитель" with raw placeholder text.
# Chain: sync → Orders.ingest_message (no event filter) → inbox kind='message'
# → Notifications.once → message_notice (author fallback + raw text).
# ----------------------------------------------------------------------------


async def test_system_item_paid_event_is_not_a_user_message(store, admin):
    """Observable contract replaces the audited assertion about inbox.kind.

    The original RED file is retained as audit_original_red.txt; retaining the
    raw event in inbox is required by CODEX_FIX_SPEC, not a regression.
    """
    from aiogram import Bot

    from app.workers.notifications import Notifications
    from tests.stateful_telegram import StatefulTelegram

    session = StatefulTelegram()
    bot = Bot("123:" + "x" * 35, session=session)
    try:
        assert Orders(store).ingest_message("ch1", _system_message("m1", "{{ITEM_PAID}}"))
        await Notifications(store, bot).once()
        notices = [v["text"] for v in session.messages.values()]
        assert len(notices) == 1
        assert "{{" not in notices[0] and "Неизвестный отправитель" not in notices[0]
        assert (
            json.loads(store.rows("SELECT payload FROM inbox WHERE key='message:m1'")[0]["payload"])["text"]
            == "{{ITEM_PAID}}"
        )
    finally:
        await session.close()


def test_message_notice_renders_placeholder_as_unknown_sender(store):
    """RED: message_notice renders '{{ITEM_PAID}}' from user=None as
    'Неизвестный отправитель' — nonsense next to the real order notification."""
    text, _markup = message_notice(_system_message("m2", "{{ITEM_PAID}}"), "ch1")
    assert "Неизвестный отправитель" not in text, "system event shown as unknown human sender"
    assert "{{" not in text, "raw template placeholder leaked to owner UI"


# ----------------------------------------------------------------------------
# DEFECT B (P1): 'Открыть результат' button after reply/mark_read always fails.
# router.py commit: → '⏳ Ожидает выполнения' + result:show:<id>, but
# store.py enqueue creates operation_receipts only for a fixed kind set that
# excludes reply/mark_read; OperationResults.show then raises Conflict.
# ----------------------------------------------------------------------------


def test_result_show_after_reply_is_available(store, admin):
    """RED: owner presses 'Открыть результат' for a queued reply — must open."""
    replies = Replies(store)
    ident = replies.enqueue(admin, "ui:token1", {"kind": "reply", "target": "ch1", "payload": {"text": "hi"}})
    results = OperationResults(store)
    text, _markup = results.show(admin, ident)  # raises Conflict today
    assert "Ожидает выполнения" in text


def test_result_show_after_mark_read_is_available(store, admin):
    """RED: same for mark_read."""
    replies = Replies(store)
    ident = replies.enqueue(admin, "ui:token2", {"kind": "mark_read", "target": "ch1", "payload": {}})
    results = OperationResults(store)
    text, _markup = results.show(admin, ident)
    assert text
