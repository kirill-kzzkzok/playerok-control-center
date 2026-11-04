"""Source-card UX for an old uncertain edit; no mutation during checking/close."""

from copy import deepcopy

import pytest

from tests.test_fix_navigation import ui as ui_fixture

ui = ui_fixture


async def test_old_edit_readonly_check_and_explicit_close_stay_in_receipt_card(ui):
    r = ui
    r.store.set_setting("playerok:account_id", r.api.account.id)
    ident = r.store.enqueue("old-edit", "edit", "lot", {"name": r.api.value["name"]}, actor=7)
    action = r.store.claim()
    r.store.mark_started(action)
    r.store.finish(ident, action["lease_token"], "UNCERTAIN", error="EDIT_NOT_CONFIRMED")
    a = r.store.setting("navigation:123:7")["id"]
    a_before = deepcopy(r.session.messages[a])
    b = (await r.bot.send_message(7, "Protected operation receipt")).message_id
    r.store.execute("UPDATE operation_receipts SET telegram_message_id=? WHERE operation_id=?", (b, ident))
    await r.ui.click("ops:show:" + ident, message_id=b)
    assert "Проверить результат на Playerok" in str(r.session.messages[b]["reply_markup"])
    await r.ui.click("ops:confirm:" + ident, message_id=b)
    assert "нет сохранённого подтверждения" in r.session.messages[b]["text"]
    assert r.store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
    await r.ui.click("ops:stop:" + ident, message_id=b)
    warning = r.session.messages[b]["text"]
    assert "не отменяет" in warning and "не подтверждает успех" in warning and "карантин" in warning
    assert r.store.rows("SELECT status FROM outbox") == [{"status": "UNCERTAIN"}]
    await r.ui.message("Проверил объявление вручную, закрываю без повтора")
    resolution = (await r.state.get_data())["resolution"]
    assert "закрыть без подтверждения успеха" in r.session.messages[b]["text"]
    await r.ui.click("ops:apply:" + resolution["nonce"], message_id=b)
    assert r.store.rows("SELECT status,error_code FROM outbox") == [
        {"status": "PERMANENT_FAILED", "error_code": "MANUALLY_STOPPED_UNCERTAIN"}
    ]
    assert r.session.messages[a] == a_before
    assert len(r.store.rows("SELECT * FROM outbox")) == 1
    await r.ui.click("attention", message_id=a)
    assert "Действия не выполнены" not in r.session.messages[a]["text"]
    assert "ops:list:ALL:0" not in str(r.session.messages[a]["reply_markup"])
    assert "В сохранённых данных проблем нет" in r.session.messages[a]["text"]


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("delivery_text", {"text": "secret"}),
        ("edit", {"logical_binding": "binding"}),
        ("edit", {"code_migration": {"product_id": "migration"}}),
    ],
)
async def test_closing_unknown_delivery_or_binding_does_not_hide_unresolved_risk(ui, kind, payload):
    r = ui
    ident = r.store.enqueue("unknown", kind, "lot", payload, actor=7)
    action = r.store.claim()
    r.store.mark_started(action)
    r.store.finish(ident, action["lease_token"], "UNCERTAIN", error="NETWORK_ERROR")
    await r.ui.click("ops:stop:" + ident)
    await r.ui.message("Закрываю запись, результат остаётся неизвестным")
    resolution = (await r.state.get_data())["resolution"]
    await r.ui.click("ops:apply:" + resolution["nonce"])
    await r.ui.click("attention")
    panel = r.store.setting("navigation:123:7")["id"]
    assert "ops:list:ALL:0" in str(r.session.messages[panel]["reply_markup"])
    assert r.store.rows("SELECT status,error_code FROM outbox") == [
        {"status": "PERMANENT_FAILED", "error_code": "MANUALLY_STOPPED_UNCERTAIN"}
    ]
