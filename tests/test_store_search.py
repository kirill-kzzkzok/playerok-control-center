"""Whole-catalog search uses real pagination and owner UI, API boundary only."""

from copy import deepcopy

import pytest

from tests.test_ux_redesign import buttons
from tests.test_ux_redesign import ux as ux_fixture

ux = ux_fixture


@pytest.fixture
async def catalog(ux):
    r = ux
    call = r.api.call
    r.catalog_calls = []
    r.store.set_setting("playerok:account_id", r.api.account.id)

    async def boundary(method, **kwargs):
        if method == "get_my_items":
            r.catalog_calls.append(kwargs)
            base = deepcopy(r.api.value)
            if not kwargs.get("after_cursor"):
                return {
                    "items": [{**base, "id": "unrelated", "name": "Other product"}],
                    "page_info": {"has_next_page": True, "end_cursor": "second"},
                }
            return {
                "items": [
                    {
                        **base,
                        "id": "match",
                        "name": "Zombie Bear [AB6]",
                        "description": "Public product\n#2163148",
                        "status": "DRAFT",
                    }
                ],
                "page_info": {"has_next_page": False},
            }
        return await call(method, **kwargs)

    r.api.call = boundary
    return r


@pytest.mark.parametrize("query", ["zombie", "AB6"])
async def test_search_finds_a_server_draft_beyond_first_page_without_manual_next(catalog, query):
    r = catalog
    await r.ui.click("il:search")
    await r.ui.message(query)
    assert "item:match" not in buttons(r.session.last_panel)
    assert "Поиск не завершён" in r.session.last_panel.text
    assert len(r.catalog_calls) == 1
    from app.services.item_filters import refresh_due_catalog

    await refresh_due_catalog(r.store, r.api)
    await r.ui.click("il:back")
    assert "item:match" in buttons(r.session.last_panel)
    assert "По ещё не загруженным" not in r.session.last_panel.text
    assert len(r.catalog_calls) == 2
    assert all(c.get("statuses") is None for c in r.catalog_calls)
    await r.ui.click("il:search")
    await r.ui.message("Bear")
    assert "item:match" in buttons(r.session.last_panel)
    assert len(r.catalog_calls) == 2
    assert not r.store.rows("SELECT * FROM outbox")


async def test_search_failure_does_not_claim_a_complete_empty_catalog(catalog):
    from app.playerok.adapter import APIError

    r = catalog
    call = r.api.call

    async def unavailable(method, **kwargs):
        if method == "get_my_items" and kwargs.get("after_cursor"):
            raise APIError("NETWORK_ERROR")
        return await call(method, **kwargs)

    r.api.call = unavailable
    await r.ui.click("il:search")
    await r.ui.message("zombie")
    assert "Поиск не завершён" in r.session.last_panel.text
    assert "Ничего не найдено" not in r.session.last_panel.text
    assert not r.store.rows("SELECT * FROM outbox")


async def test_problem_filter_uses_confirmed_operation_errors_and_can_be_removed(catalog):
    r = catalog
    r.store.enqueue("test-problem", "reply", "match", {"text": "isolated fixture"})
    r.store.execute("UPDATE outbox SET status='UNCERTAIN' WHERE operation_key='test-problem'")
    await r.ui.click("itemfilters")
    assert "il:problems" in buttons(r.session.last_panel)
    await r.ui.click("il:problems")
    assert "Поиск не завершён" in r.session.last_panel.text
    from app.services.item_filters import refresh_due_catalog

    await refresh_due_catalog(r.store, r.api)
    await r.ui.click("il:back")
    assert "item:match" in buttons(r.session.last_panel)
    assert "item:unrelated" not in buttons(r.session.last_panel)
    assert "неизвест" not in r.session.last_panel.text.lower()
    await r.ui.click("il:problems")
    assert (await r.state.get_data())["item_filters"].get("problems") is False


async def test_background_completion_does_not_shift_open_search_page(catalog):
    from app.services.item_filters import refresh_due_catalog

    r = catalog
    lots = [{"id": f"lot-{i}", "name": f"Find me {i}", "price": 100, "status": "APPROVED"} for i in range(24)]

    async def boundary(method, **kwargs):
        assert method == "get_my_items"
        if kwargs.get("after_cursor"):
            return {
                "items": [{**lots[0], "name": "Renamed"}, {"id": "later", "name": "Find me later"}],
                "page_info": {"has_next_page": False},
            }
        return {"items": lots, "page_info": {"has_next_page": True, "end_cursor": "next"}}

    r.api.call = boundary
    await r.ui.click("il:search")
    await r.ui.message("Find me")
    next_page = next(k for k in buttons(r.session.last_panel) if k.startswith("listnext:"))
    assert "item:lot-9" in buttons(r.session.last_panel)
    await refresh_due_catalog(r.store, r.api)
    await r.ui.click(next_page)
    assert "item:lot-10" in buttons(r.session.last_panel)
    assert "item:lot-9" not in buttons(r.session.last_panel)
