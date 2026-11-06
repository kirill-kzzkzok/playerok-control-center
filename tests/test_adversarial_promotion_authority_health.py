import pytest

from app.workers.promotions import PromotionWorker
from tests.test_paid_promotions import rig as rig_fixture

rig = rig_fixture


@pytest.mark.parametrize("permission", ["wrong_account", "scoped", "writes_disabled"])
async def test_health_uses_actual_account_and_scoped_authority(rig, permission):
    r = rig
    r.api.items["item"].update(status="APPROVED", priority="PREMIUM", sequence=319)
    await r.settings.target(7, "item", "include")
    revision = r.settings.get()[0]
    await r.service.approve(7, await r.service.preview(7, "item", "premium"))
    key = "promotions:launch_authority:item" if permission == "scoped" else "promotions:paid_authority"
    r.store.set_setting(key, {"revision": revision, "account_id": "other" if permission == "wrong_account" else "seller"})
    if permission == "writes_disabled":
        r.api.account.writes_enabled = False
    await PromotionWorker(r.store, r.api).once()
    assert r.store.setting("promotions:health")["status"] == (
        "SCHEDULED" if permission == "scoped" else "DEGRADED" if permission == "writes_disabled" else "DRY_RUN"
    )
    assert not r.api.charges
