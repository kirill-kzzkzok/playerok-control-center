"""Settings retain useful controls without competing technical entry screens."""

from app.database.store import now
from tests.test_fix_navigation import ui as ui_fixture
from tests.test_ux_redesign import buttons

ui = ui_fixture


async def test_settings_has_six_clear_groups_and_technical_features_are_preserved(ui):
    r = ui
    reads = len(r.api.calls)
    await r.ui.click("settings")
    visible = buttons(r.session.last_panel)
    expected = {"sp:connections", "providers", "gft:limits", "sp:notifications", "ar:menu", "tools"}
    assert {key for key in visible if not key.startswith("nav:") and key != "home"} == expected
    assert "Глобальные" in visible["ar:menu"]
    await r.ui.click("sp:connections")
    assert "gft:settings" in buttons(r.session.last_panel)
    assert "profile" in buttons(r.session.last_panel)
    await r.ui.click("tools:data")
    assert {"sp:security", "sp:fulfillment", "backup"} <= buttons(r.session.last_panel).keys()
    assert len(r.api.calls) == reads
    assert not r.store.rows("SELECT * FROM outbox")


async def test_saved_telegram_connection_is_green_only_while_fresh(ui):
    r = ui
    r.store.set_setting("gifts:connection", {"status": "OK", "at": "2020-01-01T00:00:00+00:00"})
    await r.ui.click("sp:connections")
    assert "Личный Telegram: Ожидает актуальной проверки" in r.session.last_panel.text
    r.store.set_setting("gifts:connection", {"status": "OK", "at": now()})
    await r.ui.click("sp:connections")
    assert "Личный Telegram: ✅ Подключён" in r.session.last_panel.text
    assert not r.api.calls and not r.store.rows("SELECT * FROM outbox")
