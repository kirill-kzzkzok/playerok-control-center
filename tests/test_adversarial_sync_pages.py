from copy import deepcopy

import pytest

from app.services.orders import Orders
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.sync import Synchronizer
from tests.test_product_readiness import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


@pytest.mark.parametrize("invalid", ["missing_status", "missing_item", "missing_id", "identity"])
async def test_one_bad_deal_does_not_poison_later_page_and_evidence_survives(shop, invalid):
    r = await shop()
    await setup_ui(r, "fixed_text", "delivery")
    full = r.api.order("bad")
    bad = deepcopy(full)
    if invalid == "missing_status":
        bad.pop("status")
    elif invalid == "missing_item":
        bad.pop("item")
    elif invalid == "missing_id":
        bad.pop("id")
    else:
        Orders(r.store).ingest(full)
        bad["user"]["id"] = "different-buyer"
    good = r.api.order("good")
    old = r.api.call

    async def read(method, **kwargs):
        if method == "get_deals":
            if kwargs.get("after_cursor") is None:
                return {"deals": [bad], "page_info": {"has_next_page": True, "end_cursor": "next"}}
            return r.api.page("deals", [good])
        if method == "get_chats":
            return r.api.page("chats", [])
        return await old(method, **kwargs)

    r.api.call = read
    sync = Synchronizer(r.store, r.api, Orders(r.store))
    await sync.once()
    assert r.store.rows("SELECT id FROM orders WHERE id='good'") == [{"id": "good"}]
    assert r.store.rows("SELECT 1 FROM inbox WHERE key LIKE 'sync:invalid:%'")
    await sync.process_requests()
    await EnrollmentWorker(r.store, r.api).once()
    for _ in range(8):
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    assert sum(m["chat_id"] == "chat-good" and m["text"] == "delivery" for m in r.api.messages) == 1
    if invalid == "identity":
        assert r.store.rows("SELECT problem FROM order_annotations WHERE order_id='bad'") == [{"problem": 1}]
        assert not any(m["chat_id"] == "chat-bad" for m in r.api.messages)
