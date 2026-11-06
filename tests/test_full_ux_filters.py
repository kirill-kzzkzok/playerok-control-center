from tests.test_ux_redesign import ux as ux_fixture

ux = ux_fixture


async def test_remembered_item_filter_survives_home_and_reopen(ux):
    r = ux
    await r.ui.click("items")
    await r.ui.click("itemfilter:DRAFT")
    await r.ui.click("il:remember")
    assert r.store.setting("ui:7:item_filters_pinned") is True
    await r.ui.click("home")
    await r.ui.click("items")
    assert (await r.state.get_data())["item_filters"]["status"] == "DRAFT"
    assert r.api.calls[-1][0] == "get_my_items"
    assert r.api.calls[-1][1]["statuses"] == [
        __import__("playerokapi.enums", fromlist=["ItemStatuses"]).ItemStatuses.DRAFT
    ]
