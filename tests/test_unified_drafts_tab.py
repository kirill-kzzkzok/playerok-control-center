"""One seller list, old filters cannot invisibly hide current products."""

from copy import deepcopy

import pytest

from tests.test_stage_a_controls import shop as shop_fixture
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_products_has_no_redundant_active_button(shop):
    r = await shop()
    r.store.set_setting("ui:7:item_filters", {"search": "cannot-match", "status": "SOLD"})
    r.store.set_setting("ui:7:item_filters_pinned", False)
    await r.ui.click("items")
    visible = buttons(r.session.last_panel)
    assert "item:lot" in visible
    assert {"itemfilter:ARCHIVE", "itemfilter:DRAFT", "create"} <= visible.keys()
    assert not {"itemfilter:APPROVED", "il:search", "itemfilters", "drafts"} & visible.keys()
    await r.ui.click("itemfilter:ARCHIVE")
    assert "itemfilter:APPROVED" in buttons(r.session.last_panel)
    assert not r.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("remote", [False, True])
async def test_drafts_tab_combines_local_and_remote_without_bound_duplicates_or_secrets(shop, remote):
    r = await shop()
    local = r.store.draft(7, {"name": "Local prepared account", "description": "NEVER-SHOW-THIS"})
    r.store.draft(7, {"name": "Remote historical duplicate"})
    r.store.execute("UPDATE drafts SET state='REMOTE_DRAFT' WHERE id!=?", (local,))
    if remote:
        r.api.items["server-draft"] = {**deepcopy(r.api.item), "id": "server-draft", "status": "DRAFT"}
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_my_items":
            statuses = {s.name for s in kwargs.get("statuses") or []}
            return r.api.page(
                "items", [v for v in r.api.items.values() if not statuses or v["status"] in statuses]
            )
        return await original(method, **kwargs)

    r.api.call = call
    await r.ui.click("items")
    await r.ui.click("itemfilter:DRAFT")
    visible = buttons(r.session.last_panel)
    assert "draft:" + local in visible
    assert ("item:server-draft" in visible) is remote
    assert "Локально" in visible["draft:" + local]
    if remote:
        assert "Playerok" in visible["item:server-draft"]
    assert "записей нет" not in r.session.last_panel.text
    assert "NEVER-SHOW-THIS" not in r.session.last_panel.text
    assert not any("Remote historical duplicate" in text for text in visible.values())
    assert not r.store.rows("SELECT * FROM outbox")
