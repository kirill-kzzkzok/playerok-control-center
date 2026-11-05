"""Real SafeAccount REST boundary: permits, intent ordering and unknown outcomes."""

import httpx
import pytest

from app.playerok.adapter import APIError, SafeAccount


def account(handler):
    value = SafeAccount("token=test-only", True, client=httpx.Client(transport=httpx.MockTransport(handler)))
    value.id = "seller"
    value.connection_verified = True
    value.verified_account_id = "seller"
    return value


@pytest.mark.parametrize(
    "operation,method,path",
    [
        ("discontinueItem", "discontinue_item", "discontinue"),
        ("republishItem", "republish_item", "republish"),
    ],
)
def test_native_rest_intent_precedes_one_exact_empty_post(operation, method, path):
    events = []

    def transport(request):
        assert events == ["guard", "started"]
        assert str(request.url) == f"https://playerok.com/rest-api/public/item/lot/{path}"
        assert request.method == "POST" and not request.content
        events.append("post")
        return httpx.Response(204)

    a = account(transport)
    with a.permit(
        operation,
        dispatch_guard=lambda: events.append("guard"),
        dispatch_start=lambda: events.append("started"),
    ):
        assert getattr(a, method)("lot") == {"accepted": True}
    assert events == ["guard", "started", "post"]
    a.close()


@pytest.mark.parametrize(
    "fault", ["no-permit", "no-intent", "stop", "foreign-account", "invalid-id", "timeout", "redirect"]
)
def test_native_transport_rejects_bypass_and_never_retries(fault):
    calls = []

    def transport(request):
        calls.append(request)
        if fault == "timeout":
            raise httpx.ReadTimeout("lost")
        return httpx.Response(302, headers={"location": "https://example.org"})

    a = account(transport)

    def guard():
        if fault == "stop":
            a.writes_enabled = False

    if fault == "foreign-account":
        a.verified_account_id = "other"
    with pytest.raises(APIError) as error:
        if fault == "no-permit":
            a.discontinue_item("lot")
        else:
            with a.permit(
                "discontinueItem",
                dispatch_guard=guard,
                dispatch_start=None if fault == "no-intent" else lambda: None,
            ):
                a.discontinue_item("../graphql" if fault == "invalid-id" else "lot")
    assert len(calls) == (1 if fault in {"timeout", "redirect"} else 0)
    assert error.value.uncertain == (fault in {"timeout", "redirect"})
    a.close()


async def test_visibility_capability_read_uses_actual_safe_adapter_without_write_permission():
    from app.playerok.adapter import Playerok

    seen = []
    ident = "1f1d5440-c5be-6f00-8b87-73e35f7c6486"

    def transport(request):
        import json

        payload = json.loads(request.content)
        assert str(request.url) == "https://playerok.com/graphql"
        assert payload["query"].startswith("query item(") and "mutation" not in payload["query"]
        assert payload["variables"] == {"id": ident}
        seen.append(payload["operationName"])
        return httpx.Response(
            200,
            json={
                "data": {
                    "item": {
                        "id": ident,
                        "status": "DISCONTINUED",
                        "user": {"id": "seller"},
                        "pauseAvailable": False,
                        "republishAvailable": True,
                        "mayBePublished": False,
                        "statusExpirationDate": "2026-12-01T00:00:00Z",
                    }
                }
            },
        )

    a = account(transport)
    a.writes_enabled = False
    result = await Playerok(a).call("get_item_visibility", id=ident)
    assert result["republish_available"] is True and result["may_be_published"] is False
    assert result["status"] == "DISCONTINUED" and seen == ["item"]
    a.close()
