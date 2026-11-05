from copy import deepcopy

from app.playerok.adapter import APIError
from app.services.product_readiness import ProductReadiness
from tests.test_final_outbox_resource_lanes import configure_other_item
from tests.test_unified_delivery_controls import shop as shop_fixture

shop = shop_fixture


async def test_fifty_lots_refresh_in_bounded_batches_despite_one_failed_read(shop):
    r = await shop()

    def update(id, **changes):
        r.api.mutations.append(deepcopy(changes))
        r.api.items[id].update(changes)
        return deepcopy(r.api.items[id])

    r.api.account.update_item = update
    for index in range(50):
        await configure_other_item(r, f"batch-{index:02}", "fixed_text", content="private-delivery")
    for index in range(50):
        r.store.set_setting(f"readiness:batch-{index:02}", {})
    original = r.api.call
    read = []

    async def call(method, **kwargs):
        if method == "get_item":
            read.append(kwargs["id"])
            if kwargs["id"] == "batch-00":
                raise APIError("HTTP_503")
        return await original(method, **kwargs)

    r.api.call = call
    service = ProductReadiness(r.store, r.api)
    assert await service.refresh_due() == 4
    assert set(read) == {"batch-00", "batch-01", "batch-02", "batch-03"}
    for _ in range(12):
        await service.refresh_due()
    assert set(read) == {f"batch-{i:02}" for i in range(50)}
    assert not r.api.remote.payments and not r.api.messages
