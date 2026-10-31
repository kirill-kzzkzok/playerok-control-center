import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.database.store import Store
from app.playerok.adapter import APIError, Playerok, SafeAccount
from app.playerok.thread_calls import ThreadCalls, begin_close_calls, calls_for, close_calls, drain_calls
from app.workers.outbox import Executor, OutboxWorker


async def entered(event):
    assert await asyncio.to_thread(event.wait, 3)


async def test_cancelled_read_keeps_lock_until_real_thread_finishes():
    started, release = threading.Event(), threading.Event()
    calls = []

    def read(id):
        calls.append(id)
        if id == "first":
            started.set()
            assert release.wait(5)
        return {"id": id}

    api = Playerok(SimpleNamespace(get_item=read))
    first = asyncio.create_task(api.call("get_item", id="first"))
    second = None
    try:
        await entered(started)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert api.lock.locked()
        second = asyncio.create_task(api.call("get_item", id="second"))
        await asyncio.sleep(0.01)
        assert calls == ["first"]
        assert not second.done()
    finally:
        release.set()
        await drain_calls(api)
        if second:
            assert await second == {"id": "second"}
    assert calls == ["first", "second"]
    assert not api.lock.locked()


async def test_cancelled_waiting_call_never_enters_thread():
    lock = asyncio.Lock()
    await lock.acquire()
    calls = []
    owner = ThreadCalls(lock)
    task = asyncio.create_task(owner.call(lambda cancelled: calls.append("sent")))
    try:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        lock.release()
        await owner.drain()
    assert calls == []


async def test_closing_before_first_call_rejects_late_handler_and_is_idempotent():
    read = Mock(return_value={"id": "item"})
    api = Playerok(SimpleNamespace(get_item=read))
    await close_calls(api)
    for _ in range(2):
        with pytest.raises(APIError, match="API_CLOSING"):
            await api.call("get_item", id="item")
        await close_calls(api)
    read.assert_not_called()
    assert calls_for(api).closing
    assert not calls_for(api).pending


async def test_closing_rejects_previously_queued_read_without_entering_method():
    read = Mock(return_value={"id": "item"})
    api = Playerok(SimpleNamespace(get_item=read))
    await api.lock.acquire()
    pending = asyncio.create_task(api.call("get_item", id="item"))
    try:
        await asyncio.sleep(0)
        begin_close_calls(api)
    finally:
        api.lock.release()
    with pytest.raises(APIError, match="API_CLOSING"):
        await pending
    await close_calls(api)
    read.assert_not_called()
    assert not calls_for(api).signals


async def test_closing_while_thread_pool_entry_waits_blocks_method(monkeypatch):
    queued, release = asyncio.Event(), asyncio.Event()
    read = Mock(return_value={"id": "item"})
    api = Playerok(SimpleNamespace(get_item=read))

    async def delayed_thread(function):
        queued.set()
        await release.wait()
        return function()

    monkeypatch.setattr("app.playerok.thread_calls.asyncio.to_thread", delayed_thread)
    pending = asyncio.create_task(api.call("get_item", id="item"))
    await asyncio.wait_for(queued.wait(), timeout=1)
    begin_close_calls(api)
    release.set()
    with pytest.raises(APIError, match="API_CLOSING"):
        await pending
    await close_calls(api)
    read.assert_not_called()


async def test_closed_mutation_never_reaches_http(tmp_path):
    store = Store(tmp_path / "closed.sqlite")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": {"createChatMessage": {"id": "unexpected"}}})

    account = SafeAccount(
        "token=private-cookie",
        writes_enabled=True,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    api = SimpleNamespace(account=account, lock=asyncio.Lock(), call=AsyncMock(return_value={"id": "chat"}))
    store.enqueue("one", "reply", "chat", {"text": "private"})
    begin_close_calls(api)
    try:
        assert await OutboxWorker(store, Executor(store, api, None)).once()
        assert requests == []
        assert store.rows("SELECT status,error_code FROM outbox") == [
            {"status": "PERMANENT_FAILED", "error_code": "VALIDATION_FAILED"}
        ]
    finally:
        await close_calls(api)
        account.close()
        store.close()


async def test_cancelled_auth_diagnostic_waits_before_closing_client():
    from app.auth_diagnostics import diagnose_playerok

    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    account = SimpleNamespace(id="seller", response_diagnostics=[], close=Mock())

    def read():
        started.set()
        assert release.wait(5)
        finished.set()
        return account

    account.get = read
    task = asyncio.create_task(diagnose_playerok(SimpleNamespace(), account=account))
    try:
        await entered(started)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        account.close.assert_not_called()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set()
    account.close.assert_called_once()


async def test_actual_shutdown_drains_call_before_closing_resources(tmp_path, monkeypatch):
    from app.__main__ import run

    store = Store(tmp_path / "shutdown.sqlite")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    shutdown_started = asyncio.Event()

    def read(id):
        started.set()
        assert release.wait(5)
        finished.set()
        return {"id": id}

    def close_account():
        assert finished.is_set()

    account = SimpleNamespace(id="seller", get_item=read, close=Mock(side_effect=close_account))
    account.get = lambda: account
    api = Playerok(account)
    session = SimpleNamespace(middleware=Mock(), close=AsyncMock())
    bot = SimpleNamespace(
        get_me=AsyncMock(), get_webhook_info=AsyncMock(return_value=SimpleNamespace(url="")), session=session
    )

    async def polling(*args, **kwargs):
        task = asyncio.create_task(api.call("get_item", id="item"))
        await entered(started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        shutdown_started.set()

    dispatcher = SimpleNamespace(
        fsm=Mock(),
        update=SimpleNamespace(outer_middleware=Mock()),
        message=SimpleNamespace(outer_middleware=Mock()),
        callback_query=SimpleNamespace(outer_middleware=Mock()),
        include_router=Mock(),
        start_polling=polling,
    )
    close_store = Mock(wraps=store.close)
    monkeypatch.setattr(store, "close", close_store)
    monkeypatch.setattr(store, "backup", Mock())
    monkeypatch.setattr("app.__main__.Store", lambda path: store)
    monkeypatch.setattr("app.__main__.SafeAccount", lambda *a, **k: account)
    monkeypatch.setattr("app.__main__.Playerok", lambda account: api)
    monkeypatch.setattr("app.__main__.AiohttpSession", lambda **k: session)
    monkeypatch.setattr("app.__main__.Bot", lambda *a, **k: bot)
    monkeypatch.setattr("app.__main__.Dispatcher", lambda **k: dispatcher)
    monkeypatch.setattr("app.__main__.create_router", lambda *a: object())
    monkeypatch.setattr(
        "app.__main__.Supervisor.start", lambda self, name, factory, stop: asyncio.create_task(stop.wait())
    )
    config = SimpleNamespace(
        database_path=store.path,
        playerok_header="test",
        writes_enabled=False,
        automation_enabled=False,
        use_proxy=False,
        telegram_token="test",
        owner_ids=(),
        first_start_claim=False,
        realtime_enabled=False,
    )
    running = asyncio.create_task(run(config))
    try:
        await asyncio.wait_for(shutdown_started.wait(), timeout=3)
        await asyncio.sleep(0.01)
        assert not running.done()
        with pytest.raises(APIError, match="API_CLOSING"):
            await api.call("get_item", id="late-handler")
        account.close.assert_not_called()
        session.close.assert_not_awaited()
        close_store.assert_not_called()
    finally:
        release.set()
        await asyncio.wait_for(running, timeout=3)
    account.close.assert_called_once()
    session.close.assert_awaited_once()
    close_store.assert_called_once()


@pytest.mark.parametrize("reason", ["cancel", "close"])
async def test_cancel_during_transport_throttle_blocks_http(tmp_path, monkeypatch, reason):
    store = Store(tmp_path / "throttle.sqlite")
    waiting, release = threading.Event(), threading.Event()
    requests = []

    def throttle(delay):
        waiting.set()
        assert release.wait(5)

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": {"createChatMessage": {"id": "unexpected"}}})

    account = SafeAccount(
        "token=private-cookie",
        writes_enabled=True,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    api = SimpleNamespace(account=account, lock=asyncio.Lock(), call=AsyncMock(return_value={"id": "chat"}))
    store.enqueue("one", "reply", "chat", {"text": "private"})
    task = asyncio.create_task(OutboxWorker(store, Executor(store, api, None)).once())
    monkeypatch.setattr("app.playerok.adapter.time.sleep", throttle)
    try:
        await entered(waiting)
        if reason == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            begin_close_calls(api)
        release.set()
        await drain_calls(api)
        if reason == "close":
            assert await task
        assert requests == []
        assert store.rows("SELECT status FROM outbox") == [
            {"status": "UNCERTAIN" if reason == "cancel" else "PERMANENT_FAILED"}
        ]
    finally:
        release.set()
        await drain_calls(api)
        account.close()
        store.close()


@pytest.mark.parametrize("failed", [False, True])
async def test_drain_waits_for_thread_and_abandoned_observation(failed):
    started, release = threading.Event(), threading.Event()
    owner = ThreadCalls(asyncio.Lock())
    seen = []

    def work(cancelled):
        started.set()
        assert release.wait(5)
        if failed:
            raise RuntimeError("private remote response")
        return "result"

    task = asyncio.create_task(owner.call(work, lambda result, error: seen.append((result, type(error)))))
    draining = None
    try:
        await entered(started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        draining = asyncio.create_task(owner.drain())
        await asyncio.sleep(0.01)
        assert not draining.done()
        assert seen == []
    finally:
        release.set()
        await owner.drain()
        if draining:
            await draining
    assert seen == ([(None, RuntimeError)] if failed else [("result", type(None))])
    assert not owner.pending


@pytest.mark.parametrize("remote_failure", [False, True])
async def test_cancelled_mutation_keeps_unknown_and_late_receipt_without_resend(tmp_path, remote_failure):
    store = Store(tmp_path / "cancel.sqlite")
    started, release = threading.Event(), threading.Event()
    requests = []

    def respond(request):
        requests.append(request)
        started.set()
        assert release.wait(5)
        if remote_failure:
            raise httpx.ReadTimeout("private response")
        return httpx.Response(
            200, json={"data": {"createChatMessage": {"id": "receipt", "text": "private response"}}}
        )

    account = SafeAccount(
        "token=private-cookie",
        writes_enabled=True,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    api = SimpleNamespace(account=account, lock=asyncio.Lock(), call=AsyncMock(return_value={"id": "chat"}))
    ident = store.enqueue("one", "reply", "chat", {"text": "private response"})
    worker = OutboxWorker(store, Executor(store, api, None))
    task = asyncio.create_task(worker.once())
    try:
        await entered(started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert api.lock.locked()
        assert store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"
        release.set()
        await drain_calls(api)
        assert not await worker.once()
        assert len(requests) == 1
        late = store.rows(
            "SELECT observed_status,result,error_code FROM outbox_late_results WHERE operation_id=?", (ident,)
        )
        assert len(late) == 1
        assert late[0]["observed_status"] == "UNCERTAIN"
        assert late[0]["result"] == ("{}" if remote_failure else '{"message_id":"receipt"}')
        assert "private" not in str(late)
        assert calls_for(api).pending == set()
    finally:
        release.set()
        await drain_calls(api)
        account.close()
        store.close()
