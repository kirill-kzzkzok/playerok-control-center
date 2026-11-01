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
                        "name": "Zombie Bear",
                        "description": "Public product\n#2163148",
                        "status": "DRAFT",
                    }
                ],
                "page_info": {"has_next_page": False},
            }
        return await call(method, **kwargs)

    r.api.call = boundary
    return r


@pytest.mark.parametrize("query", ["zombie", "#2163148"])
async def test_search_finds_a_server_draft_beyond_first_page_without_manual_next(catalog, query):
    r = catalog
    await r.ui.click("il:search")
    await r.ui.message(query)
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
