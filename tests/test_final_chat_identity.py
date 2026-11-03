"""A forbidden old chat is not proof that the seller session expired."""

import json

import httpx
import pytest

from app.database.store import Store
from app.playerok.adapter import APIError, Playerok, SafeAccount
from app.playerok.thread_calls import close_calls
from app.services.playerok_connection import PlayerokConnection


@pytest.mark.parametrize("method", ["get_chat_messages", "get_chat"])
@pytest.mark.parametrize(
    "viewer_status,viewer_id,restored,permission",
    [
        (200, "seller", True, True),
        (401, "seller", False, True),
        (403, "seller", False, True),
        (503, "seller", False, True),
        (200, "other-seller", False, True),
        (200, "seller", True, False),
    ],
)
async def test_chat_403_requires_new_identity_proof_before_restoring_any_write(
    tmp_path, viewer_status, viewer_id, restored, permission, method
):
    fail = False
    reads = []

    def respond(request):
        data = json.loads(request.content) if request.method == "POST" else dict(request.url.params)
        operation = data["operationName"]
        reads.append(operation)
        if operation == "viewer":
            status = viewer_status if fail else 200
            return httpx.Response(
                status,
                json={
                    "data": {
                        "viewer": {
                            "id": viewer_id if fail else "seller",
                            "username": "shop",
                            "isBlocked": False,
                        }
                    }
                },
            )
        if operation == "user":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "user": {
                            "__typename": "User",
                            "id": viewer_id if fail else "seller",
                            "profile": {"username": "shop"},
                        }
                    }
                },
            )
        assert operation in {"chatMessages", "chat"}, operation
        return httpx.Response(403, json={"message": "test chat unavailable"})

    store = Store(tmp_path / "identity.sqlite")
    account = SafeAccount(
        "token=test-only",
        writes_enabled=permission,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    account.automation_enabled = permission
    api = Playerok(account)
    connection = PlayerokConnection(store, api)
    try:
        assert (await connection.check())["status"] == "CONNECTED"
        before = reads.count("viewer")
        fail = True
        with pytest.raises(APIError) as failure:
            await api.call(
                method,
                chat_id="old-inaccessible-chat",
                **({"count": 24} if method == "get_chat_messages" else {}),
            )
        assert account.connection_verified is restored
        assert account.writes_enabled is (restored and permission)
        assert account.automation_enabled is (restored and permission)
        assert reads.count("viewer") == before + 1
        if restored:
            assert failure.value.code == "CHAT_ACCESS_FORBIDDEN"
            assert account.verified_account_id == "seller"
            assert store.setting("playerok:account_id") == "seller"
        else:
            assert connection.state["status"] != "CONNECTED"
    finally:
        await close_calls(api)
        account.close()
        store.close()
