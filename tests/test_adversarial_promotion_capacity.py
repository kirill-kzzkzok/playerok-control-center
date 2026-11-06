from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.workers.promotions import PromotionWorker
from tests.test_paid_promotions import rig as rig_fixture

rig = rig_fixture


@pytest.mark.parametrize("products", [20, 50])
async def test_scheduler_capacity_allows_two_fresh_checks_and_one_purchase_per_product(
    rig, monkeypatch, products
):
    r = rig
    r.api.balance = 10000
    clock = [datetime.now(UTC).timestamp()]

    class Date(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(clock[0], tz)

    for mod in ("app.services.promotion_settings", "app.services.paid_promotions", "app.services.finance"):
        monkeypatch.setattr(mod + ".datetime", Date)
    for mod in ("app.services.promotion_settings", "app.services.paid_promotions", "app.workers.promotions"):
        monkeypatch.setattr(mod + ".now", lambda: Date.now(UTC).isoformat())
    monkeypatch.setattr(
        "app.workers.promotions.time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0])
    )
    rev, policy, sel = r.settings.get()
    r.settings.save(
        7,
        rev,
        {**asdict(policy), "mode": "position", "threshold": 35, "daily": "10000", "monthly": "10000"},
        sel,
    )
    template = deepcopy(r.api.items["item"])
    for n in range(products):
        ident = f"p-{n:02}"
        r.api.items[ident] = {
            **deepcopy(template),
            "id": ident,
            "status": "APPROVED",
            "priority": "PREMIUM",
            "sequence": 319,
        }
        await r.settings.target(7, ident, "include")
        await r.service.approve(7, await r.service.preview(7, ident, "premium"))
    r.service.arm(7, r.settings.get()[0])
    worker = PromotionWorker(r.store, r.api)
    for _ in range(8):
        await worker.once()
        for __ in range(products):
            if not await r.worker.once():
                break
        clock[0] += 30
    assert len(r.api.charges) == products, (
        r.store.rows("SELECT status,error_code FROM outbox"),
        r.store.rows("SELECT result FROM promotion_evaluations ORDER BY id DESC LIMIT 1"),
    )
    assert len({c["item_id"] for c in r.api.charges}) == products
    assert not r.store.rows("SELECT id FROM outbox WHERE status='UNCERTAIN'")
