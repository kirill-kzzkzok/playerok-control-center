import asyncio

import httpx
import pytest

from app.live_test import mutate
from app.playerok.adapter import APIError, SafeAccount


def test_query_name_cannot_disguise_mutation():
    def handle(request):
        pytest.fail("Mutation must be blocked before network")

    account = SafeAccount("token=private", client=httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(APIError, match="MUTATION_DISGUISED_AS_QUERY"):
        account.request(
            "post",
            account.base_url + "/graphql",
            {},
            {"operationName": "viewer", "query": 'mutation viewer { removeItem(id: "x") }'},
        )


async def test_permit_must_be_acquired_in_request_thread():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"data": {"removeItem": True}})

    account = SafeAccount(
        "token=private", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(handle))
    )
    with account.permit("removeItem"), pytest.raises(APIError, match="WRITE_NOT_AUTHORIZED"):
        await asyncio.to_thread(account.remove_item, id="controlled-id")
    assert not calls
    assert await mutate(account, "removeItem", "remove_item", id="controlled-id")
    assert len(calls) == 1


async def test_multipart_update_uses_real_upload_path():
    requests = []

    def handle(request):
        requests.append(request)
        assert b"variables.addedAttachments.0" in request.content
        assert b"variables.attachments.0" not in request.content
        return httpx.Response(
            200, json={"data": {"updateItem": {"id": "controlled", "__typename": "MyItem"}}}
        )

    account = SafeAccount(
        "token=private", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(handle))
    )
    await mutate(account, "updateItem", "update_item", id="controlled", add_attachments=[b"\xff\xd8\xfftest"])
    assert len(requests) == 1


def test_allowed_operation_label_cannot_hide_paid_mutation():
    def respond(request):
        pytest.fail("Untrusted mutation text must never reach network")

    account = SafeAccount(
        "token=private", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    try:
        with account.permit("updateItem"), pytest.raises(APIError, match="WRITE_QUERY_NOT_AUTHORIZED"):
            account.request(
                "post",
                account.base_url + "/graphql",
                {},
                {"operationName": "updateItem", "query": "mutation updateItem { requestWithdrawal { id } }"},
            )
    finally:
        account.close()
