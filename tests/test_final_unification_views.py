from tests.test_ux_redesign import buttons
from tests.test_ux_redesign import ux as ux_fixture

ux = ux_fixture


async def test_category_has_one_named_picker_and_clear_in_same_form(ux):
    r = ux
    await r.state.update_data(item_filters={"category_id": "saved-category"})
    await r.ui.click("itemfilters")
    assert "il:category" not in buttons(r.session.last_panel)
    await r.ui.click("il:pickcategory")
    assert "il:clearcategory" in buttons(r.session.last_panel)
    await r.ui.click("il:clearcategory")
    assert not (await r.state.get_data()).get("item_filters", {}).get("category_id")


async def test_diagnostics_has_short_service_states_not_raw_dicts(ux):
    r = ux
    r.store.set_setting("sync:health", {"status": "OK", "internal_key": "private-internal"})
    await r.ui.click("diagnostics")
    assert "private-internal" not in r.session.last_panel.text
    assert "'status':" not in r.session.last_panel.text
    assert "Playerok:" in r.session.last_panel.text


async def test_status_is_standalone_with_close_and_keeps_open_panel(ux):
    r = ux
    r.store.set_setting("playerok:account_id", r.api.account.id)
    await r.ui.click("settings")
    before = r.store.setting("navigation:123:7")
    before_state = await r.state.get_data()
    count = len(r.session.calls)
    await r.ui.message("/status")
    report = r.session.last_panel
    assert report.__api_method__ == "sendMessage"
    assert buttons(report) == {"status:close": "Закрыть"}
    assert "Активные товары" in report.text
    assert "Завершение:" in report.text and "Сообщение:" in report.text
    assert any(
        c.__api_method__ == "deleteMessage" and c.message_id == r.ui.counter for c in r.session.calls[count:]
    )
    report_id = len(r.session.calls)
    before_close = len(r.session.calls)
    await r.ui.click("status:close", message_id=report_id, reply_markup=report.reply_markup)
    assert [c.message_id for c in r.session.calls[before_close:] if c.__api_method__ == "deleteMessage"] == [
        report_id
    ]
    assert r.store.setting("navigation:123:7") == before
    assert await r.state.get_data() == before_state


async def test_wizard_save_exit_links_to_exact_saved_draft(ux):
    r = ux
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_games":
            return {"games": [], "page_info": {"has_next_page": False}}
        return await original(method, **kwargs)

    r.api.call = call
    await r.ui.click("create")
    target = next(k for k, v in buttons(r.session.last_panel).items() if "Сохранить и выйти" in v)
    ident = (await r.state.get_data())["draft_id"]
    await r.ui.click(target)
    assert "draft:" + ident in buttons(r.session.last_panel)
    assert "сохран" in r.session.last_panel.text.lower()
    assert not r.store.rows("SELECT * FROM outbox")


async def test_saved_legacy_policy_uses_canonical_editor_without_rewriting_authority(ux):
    from dataclasses import asdict

    from app.services.automation import Automation, DeliveryPolicy

    r = ux
    r.store.set_setting("playerok:account_id", r.api.account.id)
    await Automation(r.store, r.api).save(
        7, "lot", 0, asdict(DeliveryPolicy(mode="text", text="saved-private", after="old-thanks"))
    )
    before = r.store.rows("SELECT * FROM delivery_policies")
    await r.ui.click("auto:item:lot")
    keys = buttons(r.session.last_panel)
    assert any(k.startswith("lp:") for k in keys)
    assert not any(k.startswith("ap:") for k in keys)
    assert (await r.state.get_data())["logical_setup"]["config"]["text"] == "saved-private"
    assert r.store.rows("SELECT * FROM delivery_policies") == before
    assert not r.store.rows("SELECT * FROM outbox")
