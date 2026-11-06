from time import perf_counter

from app.services.product_readiness import ProductReadiness
from tests.test_final_modern_gift_creation import shop as shop_fixture
from tests.test_ux_redesign import buttons

shop = shop_fixture


async def test_launch_open_is_local_and_final_free_action_checks_before_one_publication(shop):
    import asyncio

    r = shop
    await r.create_and_prepare(prepare_launch=False)
    ProductReadiness(r.store, r.api).remember_item(r.api.item)
    original = r.api.call
    reads = []

    async def slow(method, **kwargs):
        reads.append(method)
        await asyncio.sleep(3)
        return await original(method, **kwargs)

    r.api.call = slow
    start = perf_counter()
    await r.ui.click("launch:from:" + r.original_draft)
    elapsed = perf_counter() - start
    assert elapsed < 0.7 and not reads, (elapsed, reads)
    commit = next(k for k in buttons(r.session.last_panel) if k.endswith(":commit"))
    assert not r.api.published and not r.remote.payments
    r.api.call = original
    await r.ui.click(commit)
    await r.ui.click(commit)
    assert r.store.rows("SELECT status FROM outbox WHERE kind='publish'") == [{"status": "PENDING"}]
    await r.worker.once()
    assert len(r.api.published) == 1 and not r.remote.payments
    assert r.store.rows("SELECT status FROM outbox WHERE kind='publish'") == [{"status": "SUCCEEDED"}]
