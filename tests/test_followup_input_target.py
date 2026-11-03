"""An unaddressed owner message must not become a reply to the last buyer."""

from copy import deepcopy
from datetime import UTC, datetime

from aiogram.types import Chat, Message, User

from tests.test_fix_observability import case as case_fixture
from tests.test_telegram_transport import production_dispatcher

case = case_fixture


async def test_two_open_input_forms_do_not_guess_target_from_last_click(case):
    r = case
    a = r.store.setting("navigation:123:7")["id"]
    await r.ui.click("editname:lot", message_id=a)
    b = (await r.bot.send_message(7, "Other card")).message_id
    await r.ui.click("compose:chat", message_id=b)
    contexts = deepcopy(r.store.rows("SELECT key,state,data FROM fsm ORDER BY key"))
    r.ui.dp = production_dispatcher(r.store, r.api)
    await r.ui.message("Text intended for product title, not buyer")
    assert not r.store.rows("SELECT * FROM outbox")
    assert r.store.rows("SELECT key,state,data FROM fsm ORDER BY key") == contexts
    assert not r.api.messages
    assert "нужной форм" in r.session.calls[-1].text

    def target(ident):
        return Message(
            message_id=ident,
            date=datetime.now(UTC),
            chat=Chat(id=7, type="private"),
            from_user=User(id=r.bot.id, first_name="Bot", is_bot=True),
        )

    await r.ui.message("Title for product only", reply_to_message=target(a))
    assert not r.store.rows("SELECT * FROM outbox")
    await r.ui.message("Intended answer to this buyer", reply_to_message=target(b))
    assert r.store.rows("SELECT kind,target FROM outbox") == [{"kind": "reply", "target": "chat"}]
    await r.worker.once()
    assert len(r.api.messages) == 1 and not await r.worker.once()
