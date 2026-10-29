import asyncio
import json
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from app.database.store import Conflict, Store, now
from app.playerok.adapter import APIError, Playerok, SafeAccount, plain
from app.playerok.fulfillment_transport import fulfillment_request
from app.services.fulfillment import Fulfillment, report_fields
from app.services.products import ValidationError
from app.services.reconciliation import Reconciliation, signature
from app.telegram.fulfillment_panel import FulfillmentPanel
from app.telegram.storage import SQLiteStorage
from app.workers.outbox import Executor, OutboxWorker
from playerokapi.enums import ItemDealStatuses
from playerokapi.parser import user_profile

VALUES = {
    "workerName": "Worker",
    "telegram": "@worker_name",
    "providerId": "provider",
    "supplierOrderNumber": "private-supplier-order",
    "price": "12,30",
    "currency": "USD",
}


class Account:
    id, writes_enabled = "seller", True

    def __init__(self):
        self.written, self.error, self.calls = False, None, []

    @contextmanager
    def permit(self, operation):
        self.calls.append(operation)
        yield

    def update_deal(self, deal_id, new_status):
        assert deal_id == "deal" and new_status is ItemDealStatuses.SENT
        if self.error:
            raise self.error
        self.written = True
        return {"id": deal_id, "status": "SENT"}

    def submit_fulfillment(self, deal_id, report):
        assert deal_id == "deal" and report["state"] in {"draft", "filled"}
        if self.error:
            raise self.error
        self.written = True
        return {"accepted": True}


class API:
    def __init__(self):
        self.account, self.lock = Account(), asyncio.Lock()
        self.mode, self.providers, self.read_error = (
            "legacy",
            [{"id": "provider", "name": "Actual provider"}],
            False,
        )
        self.order = {
            "id": "deal",
            "status": "PAID",
            "direction": "OUT",
            "has_problem": False,
            "is_automated": False,
            "created_at": now(),
            "item": {"id": "item", "seller_type": "USER", "user": {"id": "seller", "role": "USER"}},
            "chat": {"id": "chat"},
        }

    async def call(self, method, **kwargs):
        if method == "get_fulfillment_providers":
            return {"items": self.providers}
        assert kwargs == {"deal_id": "deal"}
        if method == "get_deal":
            if self.account.written and self.read_error:
                raise APIError("NETWORK_ERROR")
            order = deepcopy(self.order)
            if self.account.written:
                order["status"] = "SENT"
            return order
        if method == "get_fulfillment_requirement":
            return {"requirement": self.mode}
        if method == "get_fulfillment_entry":
            return (
                {
                    "state": "filled" if self.mode == "fields" else "draft",
                    "telegram": "worker_name",
                    "submittedAt": now(),
                }
                if self.account.written
                else {"state": "absent"}
            )
        raise AssertionError(method)


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "fulfill.sqlite")
    store.claim_first_owner(1)
    api = API()
    yield store, api, Fulfillment(store, api)
    store.close()


@pytest.mark.parametrize("mode", ["legacy", "draft", "fields"])
async def test_prepared_workflow_writes_once_then_confirms_actual_state(setup, mode):
    store, api, service = setup
    api.mode = mode
    if mode != "legacy":
        api.order["item"]["user"]["role"] = "OFFICIAL_SELLER"
    prepared = await service.prepare(1, "deal")
    values = VALUES if mode == "fields" else {}
    await service.queue(1, "deal", prepared["snapshot"], values, True, "nonce")
    assert not api.account.written
    worker = OutboxWorker(store, Executor(store, api, None))
    await worker.once()
    assert not await worker.once()
    assert api.account.calls == ["updateDeal" if mode == "legacy" else "submitFulfillmentReport"]
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "SUCCEEDED"
    assert store.rows("SELECT status FROM orders")[0]["status"] == "SENT"
    assert "private-supplier-order" not in str(store.rows("SELECT * FROM order_history"))
    assert "private-supplier-order" not in str(store.rows("SELECT * FROM audit_log"))


@pytest.mark.parametrize(
    "change",
    [
        {"role": None},
        {"role": "FUTURE_ROLE"},
        {"status": "CONFIRMED"},
        {"direction": "IN"},
        {"has_problem": True},
        {"is_automated": True},
    ],
)
async def test_no_completion_for_unknown_role_or_ineligible_deal(setup, change):
    store, api, service = setup
    if "role" in change:
        api.order["item"]["user"]["role"] = change["role"]
    else:
        api.order.update(change)
    with pytest.raises(ValidationError):
        await service.prepare(1, "deal")
    assert not store.rows("SELECT * FROM outbox")


async def test_attestation_readonly_owner_and_changed_requirement_guard(setup):
    store, api, service = setup
    prepared = await service.prepare(1, "deal")
    with pytest.raises(ValidationError):
        await service.queue(1, "deal", prepared["snapshot"], {}, False, "a")
    with pytest.raises(Conflict):
        await service.prepare(2, "deal")
    api.account.writes_enabled = False
    with pytest.raises(ValidationError):
        await service.queue(1, "deal", prepared["snapshot"], {}, True, "a")
    api.account.writes_enabled = True
    api.order["item"]["user"]["role"] = "OFFICIAL_SELLER"
    api.mode = "draft"
    with pytest.raises(Conflict):
        await service.queue(1, "deal", prepared["snapshot"], {}, True, "a")
    assert not store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("failure", ["write_timeout", "read_after_write"])
async def test_unknown_result_never_repeats_write(setup, failure):
    store, api, service = setup
    prepared = await service.prepare(1, "deal")
    await service.queue(1, "deal", prepared["snapshot"], {}, True, "a")
    if failure == "write_timeout":
        api.account.error = APIError("NETWORK_ERROR", uncertain=True)
    else:
        api.read_error = True
    worker = OutboxWorker(store, Executor(store, api, None))
    await worker.once()
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"
    assert not await worker.once()
    assert api.account.calls == ["updateDeal"]
    with pytest.raises(Conflict):
        await service.prepare(1, "deal")


async def test_delivery_target_lock_and_provider_removed_before_execution(setup):
    store, api, service = setup
    prepared = await service.prepare(1, "deal")
    ident = store.enqueue("delivery:deal", "delivery", "deal", {"inventory_id": 1})
    with pytest.raises(Conflict):
        await service.queue(1, "deal", prepared["snapshot"], {}, True, "a")
    store.execute("DELETE FROM outbox WHERE id=?", (ident,))
    api.mode, api.order["item"]["user"]["role"] = "fields", "OFFICIAL_SELLER"
    prepared = await service.prepare(1, "deal")
    await service.queue(1, "deal", prepared["snapshot"], VALUES, True, "b")
    api.providers = [{"id": "other", "name": "Different"}]
    await OutboxWorker(store, Executor(store, api, None)).once()
    assert not api.account.written and not api.account.calls
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "PERMANENT_FAILED"


@pytest.mark.parametrize(
    "change",
    [
        {"price": "NaN"},
        {"price": "0"},
        {"price": "1.234"},
        {"currency": "BYN"},
        {"workerName": "x"},
        {"telegram": "bad/name"},
        {"supplierOrderNumber": ""},
    ],
)
def test_official_form_validation(change):
    values = {**VALUES, **change}
    # The official client normalizes a t.me URL/path, while arbitrary slash input is trimmed too.
    if change == {"telegram": "bad/name"}:
        assert report_fields(values)["telegram"] == "bad"
    else:
        with pytest.raises(ValidationError):
            report_fields(values)


async def test_native_rest_allowlist_permission_and_exact_body():
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.host == "playerok.com" and request.url.path.startswith("/rest-api/public/")
        if request.method == "GET":
            return httpx.Response(200, json={"requirement": "draft"})
        assert json.loads(request.content) == {"state": "draft"}
        return httpx.Response(204)

    account = SafeAccount(
        "token=private", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    try:
        assert await Playerok(account).call("get_fulfillment_requirement", deal_id="deal") == {
            "requirement": "draft"
        }
        with pytest.raises(APIError, match="WRITE_NOT_AUTHORIZED"):
            account.submit_fulfillment("deal", {"state": "draft"})
        with pytest.raises(APIError, match="ENDPOINT_FORBIDDEN"):
            fulfillment_request(account, "post", "/withdrawals", {"state": "draft"})
        with account.permit("submitFulfillmentReport"):
            assert account.submit_fulfillment("deal", {"state": "draft"}) == {"accepted": True}
        assert len(calls) == 2
    finally:
        account.close()


def test_official_role_is_preserved_by_real_upstream_parser():
    assert plain(user_profile({"id": "seller", "role": "OFFICIAL_SELLER"}))["role"] == "OFFICIAL_SELLER"


async def test_fields_form_restores_state_and_requires_final_confirmation(setup):
    store, api, _ = setup
    api.mode, api.order["item"]["user"]["role"] = "fields", "OFFICIAL_SELLER"
    panel = FulfillmentPanel(store, api)
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=1, user_id=1))

    async def click(command):
        nonce = (await state.get_data())["fulfillment"]["nonce"]
        return await panel.callback("ff:" + nonce + ":" + command, 1, state)

    async def text(value):
        await panel.message(SimpleNamespace(text=value, from_user=SimpleNamespace(id=1)), state)

    await panel.callback("ff:start:deal", 1, state)
    with pytest.raises(ValidationError):
        await click("continue")
    await click("check")
    await click("continue")
    await text("Worker")
    await text("https://t.me/worker_name")
    await click("provider:0")
    await text("private-supplier-order")
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=1, user_id=1))
    panel = FulfillmentPanel(store, api)
    await text("12,30")
    preview, _ = await click("currency:USD")
    assert "12.30" in preview and "Actual provider" in preview
    assert not store.rows("SELECT * FROM outbox")
    nonce = (await state.get_data())["fulfillment"]["nonce"]
    await click("submit")
    with pytest.raises(ValidationError):
        await panel.callback("ff:" + nonce + ":submit", 1, state)
    assert len(store.rows("SELECT * FROM outbox")) == 1 and not api.account.written


async def test_fulfillment_locks_delivery_reservation_and_unknown_result_can_be_read_reconciled(setup):
    store, api, service = setup
    prepared = await service.prepare(1, "deal")
    ident = await service.queue(1, "deal", prepared["snapshot"], {}, True, "a")
    service.orders.orders.add_stock("item", ["private code"])
    with pytest.raises(Conflict):
        service.orders.orders.reserve("deal")
    assert store.rows("SELECT status FROM inventory")[0]["status"] == "AVAILABLE"
    assert not store.rows("SELECT * FROM reservations")
    api.account.error = APIError("NETWORK_ERROR", uncertain=True)
    await OutboxWorker(store, Executor(store, api, None)).once()
    api.account.written, api.account.error = True, None
    await Reconciliation(store, api).resolve(
        1,
        ident,
        signature(store.rows("SELECT * FROM outbox")[0]),
        "CONFIRM_READ_STATE",
        "Сверил отметку SENT по API",
    )
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "SUCCEEDED"
    assert api.account.calls == ["updateDeal"]
    assert store.rows("SELECT * FROM order_history WHERE source='MANUAL_FULFILLMENT_RESOLUTION'")


async def test_rest_redirect_error_and_request_thread_guards():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example/path"})

    account = SafeAccount(
        "token=private",
        writes_enabled=True,
        client=httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True),
    )
    try:
        with pytest.raises(APIError, match="HTTP_302"):
            account.get_fulfillment_requirement("deal")
        with account.permit("submitFulfillmentReport"), pytest.raises(APIError, match="WRITE_NOT_AUTHORIZED"):
            await asyncio.wait_for(
                asyncio.to_thread(account.submit_fulfillment, "deal", {"state": "draft"}), 1
            )
        assert len(calls) == 1
    finally:
        account.close()
