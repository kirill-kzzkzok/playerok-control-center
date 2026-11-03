"""The original wizard choice survives every old entry to the new launch."""

import pytest

from app.services.products import ValidationError
from tests.test_final_acceptance_e2e import acceptance as acceptance_fixture


@pytest.fixture
async def shop(tmp_path, monkeypatch):
    async for value in acceptance_fixture.__wrapped__(tmp_path, monkeypatch):
        yield value


@pytest.mark.parametrize("entry", ["publish:item", "payp:item:item"])
async def test_created_auto_gift_draft_keeps_delivery_mode_at_legacy_entry(shop, entry):
    h = shop
    await h.create_and_prepare(prepare_launch=False)
    await h.ui.click(entry)
    ident = (await h.state.get_data())["launch"]["id"]
    value = h.store.get_draft(ident, 7)["payload"]
    assert value["mode"] == "gifts"
    assert value["gift_code"] == "heart"
    assert not h.api.published and not h.remote.payments
    assert not h.store.rows("SELECT * FROM outbox WHERE kind IN ('publish','premium_publish')")


async def test_legacy_manual_default_cannot_erase_known_auto_intent_without_selection(shop):
    h = shop
    await h.create_and_prepare(prepare_launch=False)
    # A persisted manual launch created by the old default before the owner selected a mode.
    ident = await h.launch.open(7, "item", {"delivery": "manual"})
    draft = h.launch.load(7, ident)
    value = draft["payload"]
    value["mode"] = "manual"
    value.pop("manual_delivery_confirmed", None)  # pre-upgrade serialized launch
    h.store.update_draft(ident, 7, draft["revision"], value)
    with pytest.raises(ValidationError, match="способ выдачи"):
        await h.launch.check(7, value)
    await h.ui.click("launch:open:" + ident)
    await h.launch_action("modes")
    await h.launch_action("mode:manual")
    await h.launch_action("check")
    current = h.launch.load(7, ident)["payload"]
    assert current["ready"] and current["mode"] == "manual"
    assert current["manual_delivery_confirmed"]
    assert not h.api.published and not h.remote.payments


async def test_preupgrade_queued_manual_launch_cannot_hide_original_auto_choice(shop):
    import copy

    from app.database.store import encode
    from app.services.listing_launch import digest

    h = shop
    await h.create_and_prepare(prepare_launch=False)
    await h.ui.click("publish:item")
    await h.launch_action("modes")
    await h.launch_action("mode:manual")
    await h.launch_action("check")
    ident = (await h.state.get_data())["launch"]["id"]
    await h.launch_action("commit")
    # Emulate a persisted pre-upgrade publication: same real prepared operation,
    # but no explicit manual-choice attestation in its serialized draft/signature.
    draft = h.launch.load(7, ident)
    value = draft["payload"]
    value.pop("manual_delivery_confirmed", None)
    value["ready"]["config"] = digest({k: v for k, v in value.items() if k != "ready"})
    h.store.execute("UPDATE drafts SET payload=? WHERE id=?", (encode(value), ident))
    published = []

    def publish(**kwargs):
        published.append(kwargs)
        h.api.item["status"] = "APPROVED"
        return copy.deepcopy(h.api.item)

    h.api.account.publish_item = publish
    await h.worker.once()
    assert not published
    assert h.store.rows("SELECT status,started_at FROM outbox WHERE kind='publish'") == [
        {"status": "PERMANENT_FAILED", "started_at": None}
    ]
    assert not h.remote.payments
