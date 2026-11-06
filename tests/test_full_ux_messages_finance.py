from copy import deepcopy

from app.telegram.chat_cards import ChatCards
from tests.test_chat_cards import calls, incoming
from tests.test_chat_cards import cards as cards_fixture
from tests.test_finance_panel import FinancialAPI
from tests.test_ux_redesign import buttons

cards = cards_fixture


async def test_dispatcher_chat_history_and_media_have_working_buttons_in_one_panel(cards, monkeypatch):
    r = cards
    original = r.api.call

    async def read(method, **kwargs):
        if method == "get_chat_messages":
            older = kwargs.get("after_cursor") == "older"
            return {
                "messages": [
                    {
                        "id": "old" if older else "new",
                        "text": "old-text" if older else "new-text",
                        "user": {"id": "buyer", "username": "Buyer"},
                        "images": [] if older else [{"id": "photo", "url": "https://playerok.com/test.jpg"}],
                    }
                ],
                "page_info": {"has_next_page": not older, "end_cursor": None if older else "older"},
            }
        return await original(method, **kwargs)

    r.api.call = read
    await r.ui.click("chat:chat")
    first = r.session.sequence
    older = next(
        k for k in buttons(r.session.last_panel) if k.startswith("chatpage:") and k.endswith(":older")
    )
    await r.ui.click(older)
    assert "old-text" in r.session.last_panel.text
    newer = next(
        k for k in buttons(r.session.last_panel) if k.startswith("chatpage:") and k.endswith(":newer")
    )
    await r.ui.click(newer)
    media = next(k for k in buttons(r.session.last_panel) if k.startswith("chatmedia:"))
    from app.services.attachments import Attachments

    async def image(*args):
        return b"test image bytes"

    monkeypatch.setattr(Attachments, "remote", image)
    await r.ui.click(media)
    assert r.session.sequence == first
    assert len(calls(r, "sendMessage")) + len(calls(r, "sendPhoto")) == 1
    await r.ui.click("chat:chat")
    assert "new-text" in r.session.last_panel.text
    # Telegram cannot convert a photo back to a text message. Existing
    # navigation replaces/deletes that panel instead of accumulating media.
    assert not r.session.messages[first]["exists"]
    assert sum(value["exists"] for value in r.session.messages.values()) == 1


async def test_three_buyer_messages_and_attachment_update_one_existing_card(cards):
    r = cards
    incoming(r, 1)
    await r.notifications.once()
    first = r.store.rows("SELECT * FROM chat_cards")[0]
    for n in range(2, 5):
        value = incoming(r, n)
        value["images"] = [{"id": "image", "url": "https://playerok.com/test.jpg"}]
        from app.services.orders import Orders

        Orders(r.store).ingest_message("chat", deepcopy(value))
    await r.notifications.once()
    current = r.store.rows("SELECT * FROM chat_cards")[0]
    assert first["telegram_message_id"] == current["telegram_message_id"]
    rendered = r.session.messages[current["telegram_message_id"]]["text"]
    assert "📷" in rendered and "https://playerok.com/test.jpg" in rendered
    assert len(calls(r, "sendMessage")) == 1
    assert len(current["token"]) > 10
    assert len(ChatCards(r.store, r.bot).render(current)[1].inline_keyboard[0]) == 2


async def test_finance_withdrawal_choice_preserves_product_context(cards):
    r = cards
    boundary = FinancialAPI()
    boundary.item["id"] = "lot"
    r.api.call = boundary.call
    await r.ui.click("finance:lot")
    await r.ui.click("fin:options:lot")
    await r.ui.click("providers")
    await r.ui.click("fin:select:SBP")
    assert "finance:lot" in buttons(r.session.last_panel)
    assert "profile" not in buttons(r.session.last_panel)
    await r.ui.click("fin:refresh")
    assert "finance:lot" in buttons(r.session.last_panel)
    assert "profile" not in buttons(r.session.last_panel)
    await r.ui.click("finance:lot")
    assert "Financial fixture" in r.session.last_panel.text
    assert not r.store.rows("SELECT * FROM financial_operations")


async def test_legacy_quick_reply_routes_are_read_only_redirects(cards):
    r = cards
    for route in ("quickreplies", "quick:obsolete:send", "replypreview:obsolete"):
        await r.ui.click(route)
        assert "chats" in buttons(r.session.last_panel)
        assert not any(k.startswith(("quick:", "replypreview:")) for k in buttons(r.session.last_panel))
    assert not r.sent and not r.store.rows("SELECT * FROM outbox")
