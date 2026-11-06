import json

import httpx

from app.playerok.adapter import Playerok, SafeAccount
from app.playerok.thread_calls import close_calls


async def test_current_obtaining_query_does_not_use_broken_persisted_hash():
    requests = []

    def boundary(request):
        requests.append(request)
        query = request.url.params.get("query", "")
        if not query:
            return httpx.Response(500, json={"error": "obsolete persisted document"})
        assert request.method == "GET"
        assert "mutation" not in query
        for field in ("instructionForSeller", "agreements", "feeMultiplier", "stockType", "props"):
            assert field in query
        variables = json.loads(request.url.params["variables"])
        assert variables == {
            "pagination": {"first": 2, "after": "cursor"},
            "filter": {"gameCategoryId": "category"},
        }
        return httpx.Response(
            200,
            json={
                "data": {
                    "gameCategoryObtainingTypes": {
                        "edges": [
                            {
                                "cursor": "next",
                                "node": {
                                    "id": "gift",
                                    "name": "Gift",
                                    "gameCategoryId": "category",
                                    "feeMultiplier": 1.1,
                                    "stockType": "UNLIMITED",
                                    "instructionForSeller": "Keep owner checks",
                                    "agreements": [{"id": "terms", "description": "Seller agreement"}],
                                    "props": {"minTestimonialsForSeller": 2},
                                },
                            }
                        ],
                        "pageInfo": {"endCursor": "next", "hasNextPage": True},
                        "totalCount": 3,
                    }
                }
            },
        )

    account = SafeAccount("token=test", client=httpx.Client(transport=httpx.MockTransport(boundary)))
    api = Playerok(account)
    try:
        page = await api.call(
            "get_game_category_obtaining_types", game_category_id="category", count=2, after_cursor="cursor"
        )
        node = page["obtaining_types"][0]
        assert node["id"] == "gift" and node["game_category_id"] == "category"
        assert node["instruction_for_seller"] == "Keep owner checks"
        assert node["agreements"][0]["id"] == "terms"
        assert page["page_info"]["has_next_page"] is True
        assert len(requests) == 1
    finally:
        await close_calls(api)
        account.close()
