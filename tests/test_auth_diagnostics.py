import json
from types import SimpleNamespace

import httpx
import pytest

from app.auth_diagnostics import diagnose_playerok
from app.playerok.adapter import APIError, SafeAccount
from app.playerok.response_diagnostics import response_facts


@pytest.mark.parametrize("status", [401, 403])
def test_exact_http_status_endpoint_and_body_preserved_without_credentials(status):
    secret = "sensitive-cookie-value"
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(status, json={"message": "Unauthorized token=" + secret})
        )
    )
    account = SafeAccount("token=" + secret, client=client)
    try:
        with pytest.raises(APIError) as caught:
            account.request(
                "post",
                account.base_url + "/graphql",
                {},
                {"operationName": "viewer", "query": "query viewer { viewer { id } }"},
            )
        error = caught.value
        assert error.code == str(error) == "UNAUTHORIZED_OR_CHALLENGE"
        assert error.diagnostics["http_status"] == status
        assert error.diagnostics["endpoint"] == "https://playerok.com/graphql"
        assert error.diagnostics["operation"] == "viewer"
        assert "Unauthorized" in error.diagnostics["message"]
        assert secret not in json.dumps(error.diagnostics)
    finally:
        account.close()


def test_html_challenge_facts_never_execute_script_or_keep_auth_cookie():
    html = "<html><script>secret_script()</script><title>Verification required</title> token=private-value</html>"
    response = httpx.Response(
        403,
        text=html,
        headers={"server": "ddos-guard"},
        request=httpx.Request("GET", "https://playerok.com/graphql?token=private-value"),
    )
    value = response_facts(response, "user", ["private-value"])
    text = json.dumps(value)
    assert "Verification required" in value["response_excerpt"]
    assert "private-value" not in text and "secret_script" not in text
    assert "?" not in value["endpoint"]
    assert value["server"] == "ddos-guard"


def test_success_does_not_expose_profile_or_mutation_body():
    request = httpx.Request("POST", "https://playerok.com/graphql")
    response = httpx.Response(
        200,
        json={"data": {"viewer": {"email": "private@example.com", "token": "secret-token"}}},
        request=request,
    )
    facts = response_facts(response, "viewer")
    assert "private@example.com" not in json.dumps(facts)
    assert "secret-token" not in json.dumps(facts)
    assert facts["data_object"]
    response = httpx.Response(
        400,
        json={"errors": [{"message": "buyer delivery private code", "extensions": {"code": "BAD_REQUEST"}}]},
        request=request,
    )
    facts = response_facts(response, "createChatMessage", include_messages=False)
    assert "private code" not in json.dumps(facts)
    assert facts["errors"][0]["code"] == "BAD_REQUEST"


async def test_read_only_diagnostic_uses_real_native_queries_and_masks_data():
    calls = []

    def respond(req):
        calls.append(req)
        payload = json.loads(req.content) if req.method == "POST" else dict(req.url.params)
        operation = payload["operationName"]
        if operation == "viewer":
            return httpx.Response(
                200, json={"data": {"viewer": {"id": "seller", "username": "PrivateUsername"}}}
            )
        if operation == "user":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "user": {
                            "__typename": "User",
                            "id": "seller",
                            "profile": {"username": "PrivateUsername"},
                        }
                    }
                },
            )
        assert operation == "transactions"
        variables = json.loads(payload["variables"])
        assert variables["filter"]["userId"] == "seller"
        assert variables["pagination"]["first"] == 1
        return httpx.Response(
            200, json={"data": {"transactions": {"edges": [], "pageInfo": {"hasNextPage": False}}}}
        )

    account = SafeAccount("token=private-cookie", client=httpx.Client(transport=httpx.MockTransport(respond)))
    report = await diagnose_playerok(SimpleNamespace(), account)
    assert report["status"] == "OK" and report["mutations"] == 0
    assert [trace["operation"] for trace in report["http"]] == ["viewer", "user", "transactions"]
    assert len(calls) == 3
    assert "PrivateUsername" not in json.dumps(report)
    assert "private-cookie" not in json.dumps(report)


async def test_challenge_diagnostic_stops_without_retry_or_challenge_bypass():
    calls = []

    def respond(req):
        calls.append(req)
        return httpx.Response(403, text="Verification required")

    account = SafeAccount("token=private-cookie", client=httpx.Client(transport=httpx.MockTransport(respond)))
    report = await diagnose_playerok(SimpleNamespace(), account)
    assert report["status"] == "ERROR"
    assert report["failure"]["http_status"] == 403
    assert len(calls) == 1
