"""Fresh hydration of the same sale; retained observations never authorize send."""

import copy
import json
from types import SimpleNamespace

import pytest

from app.gifts.worker import GiftWorker
from app.services.orders import Orders
from app.workers import enrollment
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from tests.test_logical_faults import reopen
from tests.test_playerok_delivery import configure, drain
from tests.test_universal_delivery_e2e import setup_ui
from tests.test_universal_delivery_e2e import simulation as simulation_fixture


@pytest.fixture
async def shop(tmp_path):
    async for create in simulation_fixture.__wrapped__(tmp_path):
        yield create


def boundaries(r, short, full):
    original = r.api.call

    async def call(method, **kwargs):
        if method in {"get_deal", "get_delivery_deal"}:
            assert kwargs["deal_id"] == short["id"]
            r.api.calls.append((method, kwargs))
            return copy.deepcopy(short if method == "get_deal" else full)
        return await original(method, **kwargs)

    r.api.call = call


@pytest.mark.parametrize("mode,value", [("unique_stock", None), ("fixed_text", "one-private-unit")])
async def test_delayed_chat_details_resume_after_restart(shop, monkeypatch, mode, value):
    r = await shop()
    await setup_ui(r, mode, value)
    full = r.api.order("delayed-details")
    chat = full["chat"]
    full["chat"] = None
    short = copy.deepcopy(full)
    boundaries(r, short, full)
    Orders(r.store).ingest(short)
    await EnrollmentWorker(r.store, r.api).once()
    assert not r.api.messages
    assert not r.store.rows("SELECT * FROM delivery_chains")
    assert not r.store.rows("SELECT * FROM reservations")
    waiting = r.store.rows("SELECT status,error_code,due_at FROM auto_enrollment")[0]
    assert (waiting["status"], waiting["error_code"]) == ("PENDING", "DELIVERY_CHAT_NOT_YET_AVAILABLE")

    reopen(r)
    full["chat"] = chat
    assert await EnrollmentWorker(r.store, r.api).once() == 0
    monkeypatch.setattr(enrollment, "time", SimpleNamespace(time=lambda: waiting["due_at"] + 1))
    for _ in range(8):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "SUCCEEDED"}]
    assert len(r.api.messages) == 2


@pytest.mark.parametrize("mode,value", [("unique_stock", None), ("fixed_text", "one-private-unit")])
async def test_non_gift_chain_hydrates_missing_chat_and_item_owner(shop, mode, value):
    r = await shop()
    await setup_ui(r, mode, value)
    full = r.api.order("incomplete-paid")
    short = copy.deepcopy(full)
    short["chat"] = None
    short["item"].update(user=None, description=None, category=None)
    boundaries(r, short, full)
    Orders(r.store).ingest(short)
    for _ in range(8):
        await EnrollmentWorker(r.store, r.api).once()
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "SUCCEEDED"}]
    assert len(r.api.messages) == 2
    assert any(method == "get_delivery_deal" for method, _ in r.api.calls)


@pytest.mark.parametrize("actual_username", [True, False])
async def test_gift_missing_short_username_reads_full_and_never_uses_retained_username(shop, actual_username):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    full = r.api.order("incomplete-gift")
    Orders(r.store).ingest(full)  # A previous observation is deliberately available.
    short = copy.deepcopy(full)
    short.pop("obtaining_fields")
    if not actual_username:
        full["obtaining_fields"] = []
    boundaries(r, short, full)
    Orders(r.store).ingest(short)
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once()
    assert any(method == "get_delivery_deal" for method, _ in r.api.calls)
    assert len(r.api.remote.payments) == int(actual_username)
    if actual_username:
        assert r.store.rows("SELECT state FROM gift_jobs") == [{"state": "SUCCEEDED"}]


async def test_partial_storage_retains_observations_and_records_absent_vs_null(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    full = r.api.order("retain")
    Orders(r.store).ingest(full)
    short = copy.deepcopy(full)
    short.pop("obtaining_fields")
    short["item"]["description"] = None
    Orders(r.store).ingest(short)
    stored = json.loads(r.store.rows("SELECT payload FROM orders WHERE id='retain'")[0]["payload"])
    assert stored["obtaining_fields"] == full["obtaining_fields"]
    assert stored["item"]["description"] == full["item"]["description"]
    assert stored["_retained_fields"]["obtaining_fields"] == "absent"
    assert stored["_retained_fields"]["item.description"] == "null"
    assert not r.api.remote.payments


@pytest.mark.parametrize("conflict", ["buyer", "seller", "price", "item", "recipient", "attributes"])
async def test_conflicting_full_deal_never_authorizes_a_delivery(shop, conflict):
    r = await shop()
    await setup_ui(r, "fixed_text", "one-private-unit")
    full = r.api.order("conflicting-paid")
    short = copy.deepcopy(full)
    short["chat"] = None  # Trigger the full contract without losing known identity evidence.
    if conflict == "buyer":
        full["user"]["id"] = "other-buyer"
    elif conflict == "seller":
        full["item"]["user"]["id"] = "other-seller"
    elif conflict == "price":
        full["item"]["price"] += 1
    elif conflict == "item":
        full["item"]["id"] = "other-item"
    elif conflict == "recipient":
        full["obtaining_fields"][0]["value"] = "@otherbuyer"
    else:
        short["item"]["attributes"] = {"amount": "confirmed"}
        full["item"]["attributes"] = {"amount": "different"}
    boundaries(r, short, full)
    Orders(r.store).ingest(short)
    await EnrollmentWorker(r.store, r.api).once()
    ChainWorker(r.store, r.api).once()
    await r.worker.once()
    assert not r.store.rows("SELECT * FROM delivery_chains")
    assert not r.api.messages and not r.api.remote.payments


@pytest.mark.parametrize("reply,fulfill", [(True, False), (False, True)])
async def test_platform_hydrates_only_the_fields_needed_for_independent_followup(shop, reply, fulfill):
    r = await shop()
    await configure(r, reply=reply, fulfill=fulfill)
    full = r.api.order("platform-incomplete")
    full["is_automated"] = True
    if not reply:
        full["chat"] = None
    short = copy.deepcopy(full)
    if reply:
        short["item"]["user"] = None
    else:
        short["item"]["user"].pop("role")
    update = r.api.account.update_deal

    def updated(deal_id, new_status):
        result = update(deal_id, new_status)
        short["status"] = full["status"]
        return result

    r.api.account.update_deal = updated
    boundaries(r, short, full)
    Orders(r.store).ingest(short)
    await drain(r)
    assert any(method == "get_delivery_deal" for method, _ in r.api.calls)
    assert r.store.rows("SELECT status FROM delivery_chains") == [{"status": "SUCCEEDED"}]
    assert len(r.api.messages) == int(reply)
    assert full["status"] == ("SENT" if fulfill else "PAID")
    assert not r.api.remote.payments and not r.store.rows("SELECT * FROM reservations")
