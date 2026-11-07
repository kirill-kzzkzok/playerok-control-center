"""Final two regressions through production Dispatcher with isolated SQLite."""

import json

import pytest

from app.database.store import encode
from app.services.orders import Orders
from tests.test_chat_cards import cards as cards_fixture
from tests.test_chat_cards import incoming
from tests.test_universal_delivery_e2e import click, setup_ui
from tests.test_universal_delivery_e2e import simulation as simulation_fixture
from tests.test_ux_redesign import buttons

cards = cards_fixture
simulation = simulation_fixture


async def photo_history(r):
    for i in range(1, 18):
        message = incoming(r, i)
        message["images"] = [{"id": f"image-{i}", "url": f"https://cdn.playerok.com/{i}.jpg"}]
        Orders(r.store).ingest_message("chat", message)
    await r.notifications.once()
    await r.ui.click("chat:chat")


@pytest.mark.parametrize("older,restart", [(False, False), (True, False), (True, True)])
async def test_saved_photo_uses_exact_message_without_page_cursor(cards, monkeypatch, older, restart):
    from app.services.attachments import Attachments

    r = cards
    await photo_history(r)
    if older:
        await click(r, suffix=":older")
    action = next(k for k in buttons(r.session.last_panel) if k.startswith("chatmedia:"))
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    state = FSMContext(r.ui.dp.storage, StorageKey(bot_id=123, chat_id=7, user_id=7))
    view = (await state.get_data())["chat_view"]
    selected = view["media"][0]["message_id"]
    expected = json.loads(
        r.store.rows("SELECT payload FROM chat_messages WHERE id=?", (selected,))[0]["payload"]
    )["images"][0]["url"]
    incoming(r, 100)  # Must not shift the opened page's image identity.
    if restart:
        from tests.test_telegram_transport import production_dispatcher

        r.ui.dp = production_dispatcher(r.store, r.api)
    downloads, reads = [], []

    async def image(self, url):
        downloads.append(url)
        return b"verified-image"

    async def offline(method, **kwargs):
        reads.append((method, kwargs))
        raise ConnectionError("Playerok unavailable")

    monkeypatch.setattr(Attachments, "remote", image)
    r.api.call = offline
    await r.ui.click(action)
    assert downloads == [expected]
    assert not reads and not r.sent
    assert not r.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize("handler", ["telegram_gifts", "unique_stock", "fixed_text"])
@pytest.mark.parametrize("logical,projection", [(True, True), (True, False), (False, True), (False, False)])
@pytest.mark.parametrize("linked", [False, True])
async def test_divergent_enabled_product_can_be_finally_stopped(
    simulation, handler, logical, projection, linked
):
    from copy import deepcopy

    from app.workers.logical_products import LogicalProductWorker

    r = await simulation(gift=handler == "telegram_gifts")
    await setup_ui(r, handler, "gift: heart" if handler == "telegram_gifts" else "fixed-content")
    if linked:
        r.api.item["status"] = "SOLD"
        r.api.items["second"] = {**deepcopy(r.api.item), "id": "second", "status": "APPROVED"}
        await LogicalProductWorker(r.store, r.api).once()
        assert len(r.store.rows("SELECT * FROM logical_listings")) == 2
    r.store.execute("UPDATE logical_products SET enabled=?", (logical,))
    if handler == "telegram_gifts":
        r.store.execute("UPDATE gift_listings SET enabled=?", (projection,))
    else:
        r.store.execute(
            "UPDATE delivery_policies SET payload=json_set(payload,'$.enabled',json(?))",
            (json.dumps(projection),),
        )
        if linked and not logical and projection:
            r.store.execute(
                "UPDATE delivery_policies SET payload=json_set(payload,'$.enabled',json('false')) WHERE item_id='second'"
            )
    unknown = []
    if handler == "telegram_gifts" and logical and not projection:
        r.store.set_setting("gifts:stopped", True)
        r.store.execute("UPDATE outbox SET status='UNCERTAIN' WHERE kind='edit'")
        unknown = r.store.rows("SELECT * FROM outbox WHERE status='UNCERTAIN'")
        assert unknown
    before = r.store.rows("SELECT * FROM logical_products")
    for route in ["lp:item:", "lp:details:"]:
        await r.ui.click(route + ("second" if linked else "lot"))
        keys = buttons(r.session.last_panel)
        assert any(k.endswith(":stop") for k in keys) == bool(logical or projection)
        assert any(k.endswith(":arm") for k in keys) == (not logical and not projection)
        assert ("не согласованы" in r.session.last_panel.text) == (logical != projection)
        assert r.store.rows("SELECT * FROM logical_products") == before
    if logical or projection:
        await click(r, suffix=":stop")
        assert f"Затронуто объявлений: {2 if linked else 1}" in r.session.last_panel.text
        confirm = next(k for k in buttons(r.session.last_panel) if k.endswith(":stop_confirm"))
        await r.ui.click(confirm)
        after = r.store.rows("SELECT * FROM logical_products")
        await r.ui.click(confirm)
        assert r.store.rows("SELECT * FROM logical_products") == after
        assert after[0]["enabled"] == 0
        if handler == "telegram_gifts":
            assert not any(row["enabled"] for row in r.store.rows("SELECT enabled FROM gift_listings"))
        else:
            assert not any(
                json.loads(row["payload"])["enabled"]
                for row in r.store.rows("SELECT payload FROM delivery_policies")
            )
    assert not r.api.messages and not r.api.remote.payments
    if unknown:
        assert r.store.setting("gifts:stopped") is True
        assert r.store.rows("SELECT * FROM outbox WHERE status='UNCERTAIN'") == unknown


@pytest.mark.parametrize(
    "fault",
    [
        "deleted",
        "removed",
        "changed",
        "missing",
        "foreign_chat",
        "account",
        "stored_account",
        "stale",
        "old_metadata",
    ],
)
async def test_saved_photo_rejects_stale_or_changed_evidence(cards, monkeypatch, fault):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    from app.services.attachments import Attachments

    r = cards
    await photo_history(r)
    state = FSMContext(r.ui.dp.storage, StorageKey(bot_id=123, chat_id=7, user_id=7))
    view = (await state.get_data())["chat_view"]
    action = f"chatmedia:{view['nonce']}:0"
    ident = view["media"][0]["message_id"]
    row = r.store.rows("SELECT payload FROM chat_messages WHERE id=?", (ident,))[0]
    message = json.loads(row["payload"])
    if fault == "deleted":
        message["deleted_at"] = "2026-11-07T12:00:00Z"
    elif fault == "removed":
        message["images"] = []
    elif fault == "changed":
        message["images"][0]["url"] = "https://evil.example/private"
    r.store.execute("UPDATE chat_messages SET payload=? WHERE id=?", (encode(message), ident))
    if fault == "missing":
        r.store.execute("DELETE FROM chat_messages WHERE id=?", (ident,))
    elif fault == "foreign_chat":
        r.store.execute("UPDATE chat_messages SET chat_id='other' WHERE id=?", (ident,))
    elif fault == "account":
        r.api.account.id = "other"
    elif fault == "stored_account":
        r.store.set_setting("playerok:account_id", "other")
    elif fault == "stale":
        await click(r, suffix=":older")
    elif fault == "old_metadata":
        view["media"][0].pop("image")
        await state.update_data(chat_view=view)
    downloads, reads = [], []

    async def image(self, url):
        downloads.append(url)
        raise AssertionError("unsafe image download")

    async def offline(method, **kwargs):
        reads.append(method)
        raise AssertionError("no cursor-based fallback")

    monkeypatch.setattr(Attachments, "remote", image)
    r.api.call = offline
    await r.ui.click(action)
    assert not downloads and not reads and not r.sent
    assert not r.store.rows("SELECT * FROM outbox")


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.playerok.com/a",
        "https://127.0.0.1/a",
        "https://playerok.com.evil.example/a",
        "https://user@playerok.com/a",
    ],
)
async def test_saved_photo_keeps_real_attachment_url_guard(cards, monkeypatch, url):
    r = cards
    message = incoming(r, 1)
    message["images"] = [{"id": "image", "url": url}]
    Orders(r.store).ingest_message("chat", message)
    await r.notifications.once()
    await r.ui.click("chat:chat")
    action = next(k for k in buttons(r.session.last_panel) if k.startswith("chatmedia:"))

    def forbidden_client(**kwargs):
        raise AssertionError("SSRF must be rejected before HTTP")

    monkeypatch.setattr("app.services.attachments.httpx.AsyncClient", forbidden_client)
    await r.ui.click(action)
    assert "Недопустимый адрес" in r.session.last_panel.text
    assert not r.sent
