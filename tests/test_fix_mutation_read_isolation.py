"""Production SafeAccount + ThreadCalls + durable fencing, HTTP boundary simulated."""

import asyncio
import threading

import httpx
import pytest

from app.database.store import Store
from app.playerok.adapter import QUERIES, Playerok, SafeAccount
from app.playerok.thread_calls import begin_close_calls, calls_for, drain_calls
from app.workers.outbox import Executor


@pytest.mark.parametrize("operation", ["increaseItemPriorityStatus", "updateItem", "createChatMessage"])
@pytest.mark.parametrize("expired", [False, True])
async def test_slow_authorized_post_does_not_delay_paid_read_and_cancel_keeps_writer(
    tmp_path, operation, expired
):
    store = Store(tmp_path / "transport.sqlite")
    entered, release = threading.Event(), threading.Event()
    dispatched = []

    def respond(request):
        if request.method == "POST":
            dispatched.append(request)
            entered.set()
            assert release.wait(25)
        return httpx.Response(200, json={"data": {"viewer": {"id": "seller"}, "deal": {"id": "paid"}}})

    account = SafeAccount(
        "token=test", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    account.id = "seller"
    api = Playerok(account)
    ident = store.enqueue("original", "edit", "lot", {"name": "confirmed"})
    action = store.claim(operation_id=ident)
    account.get_deal = lambda deal_id: account.request(
        "get",
        account.base_url + "/graphql",
        {},
        {"operationName": "viewer", "query": "query { viewer { id } }"},
    ).json()["data"]["deal"]

    def write(cancelled):
        with account.permit(
            operation,
            dispatch_guard=lambda: Executor(store, api, None).dispatch_guard(action),
            dispatch_start=lambda: store.mark_started(action),
        ):
            return account.request(
                "post",
                account.base_url + "/graphql",
                {},
                {"operationName": operation, "query": QUERIES[operation], "variables": {}},
            ).json()

    late = []

    def observe(result, error):
        late.append((result, error))
        store.record_late_result(ident, action["lease_token"], "UNCERTAIN", {"remote_id": "lot"})

    calls = calls_for(api)
    first = asyncio.create_task(calls.call(write, on_abandoned=observe))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert await asyncio.wait_for(api.call("get_deal", deal_id="paid"), 1) == {"id": "paid"}
        assert not release.is_set() and len(dispatched) == 1
        second = asyncio.create_task(calls.call(write))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert api.lock.locked()
        if expired:
            store.execute("UPDATE outbox SET lease_until=0 WHERE id=?", (ident,))
            store.recover()
        else:
            store.finish(ident, action["lease_token"], "UNCERTAIN", error="CANCELLED_AFTER_DISPATCH")
        # A real20-second blocked PREMIUM POST; independent read already completed.
        if operation == "increaseItemPriorityStatus":
            await asyncio.sleep(20)
        assert len(dispatched) == 1 and not second.done()
        begin_close_calls(api)
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await drain_calls(api)
        if second:
            await asyncio.gather(second, return_exceptions=True)
        if late:
            assert store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": "UNCERTAIN"}]
            assert len(store.rows("SELECT * FROM outbox_late_results WHERE operation_id=?", (ident,))) == 1
        account.close()
        store.close()
    assert len(dispatched) == 1 and len(late) == 1
