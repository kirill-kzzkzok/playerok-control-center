from types import SimpleNamespace

import pytest

from app.playerok.adapter import APIError, Playerok


@pytest.mark.parametrize(
    "method,result",
    [
        ("get_item", None),
        ("get_item", {}),
        ("get_game_category", {"name": "missing id"}),
        ("get_transaction_providers", [None]),
        ("get_item_priority_statuses", {"id": "not a list"}),
        ("get_games", None),
    ],
)
async def test_read_response_cannot_pass_as_success_when_incomplete(method, result):
    api = Playerok(SimpleNamespace(**{method: lambda **kwargs: result}))
    with pytest.raises(APIError, match="UPSTREAM_INCOMPLETE_RESPONSE"):
        await api.call(method)


@pytest.mark.parametrize(
    "page,code",
    [
        ({"items": [], "page_info": None}, "INCOMPLETE_PAGE_INFO"),
        ({"items": [], "page_info": {}}, "INCOMPLETE_PAGE_INFO"),
        ({"items": [None], "page_info": {"has_next_page": False}}, "INCOMPLETE_PAGE_ENTRY"),
        ({"items": [{"id": "one"}], "page_info": {"has_next_page": True}}, "INVALID_PAGINATION_CURSOR"),
        ({"items": [], "page_info": {"has_next_page": "false"}}, "INCOMPLETE_PAGE_INFO"),
    ],
)
async def test_broken_page_never_yielded_to_synchronizer(page, code):
    api = Playerok(SimpleNamespace())

    async def call(*args, **kwargs):
        return page

    api.call = call
    processed = []
    with pytest.raises(APIError, match=code):
        async for values in api.pages("get_my_items", "items"):
            processed.extend(values)
    assert not processed


async def test_repeated_cursor_stops_before_duplicate_page_is_processed():
    api = Playerok(SimpleNamespace())
    calls = []

    async def call(*args, **kwargs):
        calls.append(kwargs.get("after_cursor"))
        return {"items": [{"id": "one"}], "page_info": {"has_next_page": True, "end_cursor": "same"}}

    api.call = call
    seen = []
    with pytest.raises(APIError, match="INVALID_PAGINATION_CURSOR"):
        async for values in api.pages("get_my_items", "items"):
            seen.extend(values)
    assert calls == [None, "same"]
    assert len(seen) == 1
