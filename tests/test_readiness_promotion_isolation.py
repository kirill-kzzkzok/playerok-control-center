"""RUB promotion uncertainty is quarantined without claiming Gift delivery is stopped."""

from app.gifts.worker import GiftWorker
from app.services.orders import Orders
from app.services.product_readiness import ProductReadiness
from tests.test_unified_delivery_controls import shop as shop_fixture
from tests.test_universal_delivery_e2e import setup_ui

shop = shop_fixture


async def test_unknown_rub_promotion_does_not_mask_or_replay_gift_delivery(shop):
    r = await shop(gift=True)
    await setup_ui(r, "telegram_gifts", "gift: heart")
    ident = r.store.enqueue("isolated-unknown", "premium_bump", "lot", {}, actor=7)
    action = r.store.claim()
    assert action["id"] == ident
    r.store.mark_started(action)
    r.store.finish(ident, action["lease_token"], "UNCERTAIN", error="NETWORK_ERROR")
    result = ProductReadiness(r.store, r.api).snapshot("lot", item=r.api.item)
    assert result["ready"], result["problems"]
    Orders(r.store).ingest(r.api.order("future-sale"))
    await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
    await r.worker.once(kinds=("gift_send",))
    assert len(r.api.remote.payments) == 1
    assert r.store.rows("SELECT status FROM outbox WHERE id=?", (ident,)) == [{"status": "UNCERTAIN"}]
    assert not r.api.messages
