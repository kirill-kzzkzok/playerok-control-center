import asyncio
from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from app.database.store import Conflict, Store
from app.playerok.adapter import APIError
from app.services.attachments import Attachments
from app.services.products import Products, save_image
from app.workers.outbox import Executor, OutboxWorker


class FakeAccount:
    id = "seller"
    writes_enabled = True

    def __init__(self):
        self.calls = []
        self.published = {}
        self.created = {}

    @contextmanager
    def permit(self, operation):
        self.calls.append(operation)
        yield

    def create_item(self, **kwargs):
        result = SimpleNamespace(
            id="remote1",
            status="DRAFT",
            user={"id": self.id},
            category={"id": kwargs["game_category_id"]},
            obtaining_type={"id": kwargs["obtaining_type_id"]},
            name=kwargs["name"],
            description=kwargs["description"],
            price=kwargs["price"],
            raw_price=kwargs["price"],
            attachments=[{"id": f"image-{index}"} for index, _ in enumerate(kwargs.get("attachments", []))],
        )
        self.created[result.id] = vars(result)
        return result

    def publish_item(self, **kwargs):
        self.published[kwargs["item_id"]] = "PENDING_APPROVAL"
        return SimpleNamespace(id=kwargs["item_id"], status="PENDING_APPROVAL")

    def update_item(self, **kwargs):
        return SimpleNamespace(**kwargs)

    def remove_item(self, **kwargs):
        return True

    def send_message(self, **kwargs):
        return SimpleNamespace(id="message1")

    def mark_chat_as_read(self, **kwargs):
        return SimpleNamespace(id=kwargs["chat_id"], unread_messages_counter=0)


class FakeAPI:
    def __init__(self):
        self.account = FakeAccount()
        self.lock = asyncio.Lock()
        self.quote = {"id": "free", "type": "DEFAULT", "price": 0}
        self.item_owner = "seller"

    async def call(self, method, **kwargs):
        if method == "get_game_category":
            return {"id": kwargs["id"], "options": []}
        if method == "get_item":
            if kwargs["id"] in getattr(self.account, "created", {}):
                result = dict(self.account.created[kwargs["id"]])
                result["status"] = self.account.published.get(kwargs["id"], result["status"])
                return result
            published = getattr(self.account, "published", {}).get(kwargs["id"])
            return {
                "id": kwargs["id"],
                "user": {"id": self.item_owner},
                "status": published or "DRAFT",
                "price": 100,
                "raw_price": 100,
            }
        if method == "get_item_priority_statuses":
            return [self.quote]
        if method == "get_chat":
            assert "chat_id" in kwargs
            return {"id": kwargs["chat_id"]}
        raise AssertionError(method)

    async def pages(self, method, collection, **kwargs):
        if method == "get_game_category_obtaining_types":
            yield [{"id": "o"}]
        else:
            assert method in {"get_game_category_data_fields", "get_game_category_agreements"}
            yield []


async def queue_manual_publication(store, api, target, owner=1, *, publication="DEFAULT", quote_id=None):
    """Prepare real manual launch authority before testing a publication receipt."""
    from dataclasses import asdict

    from app.gifts.service import GiftService
    from app.gifts.settings import GiftSettings
    from app.services.listing_launch import ListingLaunch
    from app.services.promotion_settings import PromotionSettings

    store.claim_first_owner(owner)
    settings = GiftSettings(
        1, "test-only", "unused.session", "category", "obtaining", "username", False, 100, 100
    )
    api.gift_settings = settings
    service = ListingLaunch(store, api, GiftService(store, api, settings))
    ident = await service.open(owner, target, {"delivery": "manual"})
    draft = service.load(owner, ident)
    value = draft["payload"]
    value.update(mode="manual", manual_delivery_confirmed=True, publication=publication, quote_id=quote_id)
    value["promotion_policy"] = asdict(PromotionSettings(store, api).get()[1])
    value["ready"] = await service.check(owner, value)
    store.update_draft(ident, owner, draft["revision"], value)
    return await service.queue(owner, ident, service.load(owner, ident)["revision"])


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "operations.sqlite")
    api = FakeAPI()
    products = Products(store, api)
    yield store, api, products, OutboxWorker(store, Executor(store, api, products))
    store.close()


async def test_create_double_click_and_publish(setup):
    store, api, products, worker = setup
    ident = store.draft(
        1,
        {
            "category_id": "c",
            "obtaining_type_id": "o",
            "name": "Test product",
            "description": "Test description",
            "price": 100,
            "fields": {"__comment": "Test buyer delivery data"},
            "photos": [],
        },
    )
    results = await asyncio.gather(
        products.queue_create(ident, 1, 0), products.queue_create(ident, 1, 0), return_exceptions=True
    )
    assert sum(isinstance(r, Conflict) for r in results) == 1
    await worker.once()
    assert api.account.calls == ["createItem"]
    assert not await worker.once()
    assert store.rows("SELECT remote_id FROM bindings")[0]["remote_id"] == "remote1"
    await queue_manual_publication(store, api, "remote1")
    await worker.once()
    assert api.account.calls == ["createItem", "publishItem"]


async def test_stale_paid_quote_blocks_before_mutation(setup):
    store, api, _, worker = setup
    await queue_manual_publication(store, api, "remote1")
    api.quote["price"] = 1
    await worker.once()
    assert not api.account.calls
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "PERMANENT_FAILED"


@pytest.mark.parametrize("kind,payload", [("edit", {"price": 100}), ("delete", {})])
async def test_foreign_item_write_forbidden(setup, kind, payload):
    store, api, _, worker = setup
    api.item_owner = "another seller"
    store.enqueue("op", kind, "remote1", payload)
    await worker.once()
    assert not api.account.calls


async def test_edit_delete_reply_dispatch_real_method_signatures(setup):
    store, api, _, worker = setup
    for kind, payload in [("edit", {"price": 200}), ("delete", {}), ("reply", {"text": "hello"})]:
        store.enqueue(kind, kind, "remote1", payload)
        await worker.once()
    assert api.account.calls == ["updateItem", "removeItem", "createChatMessage"]
    assert all(r["status"] == "SUCCEEDED" for r in store.rows("SELECT status FROM outbox"))


async def test_response_lost_after_remote_create_not_repeated(setup):
    store, api, products, worker = setup

    def create(**kwargs):
        raise APIError("NETWORK_ERROR", uncertain=True)

    api.account.create_item = create
    ident = store.draft(
        1,
        {
            "category_id": "c",
            "obtaining_type_id": "o",
            "name": "Test product",
            "description": "Test description",
            "price": 100,
            "fields": {"__comment": "Test buyer delivery data"},
            "photos": [],
        },
    )
    await products.queue_create(ident, 1, 0)
    await worker.once()
    assert not await worker.once()
    assert api.account.calls == ["createItem"]
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"


async def test_replace_images_checks_fresh_attachment_ids_and_passes_bytes(setup, tmp_path, monkeypatch):
    store, api, _, worker = setup
    images = Attachments(tmp_path / "uploads")
    monkeypatch.setattr("app.workers.outbox.Attachments", lambda: images)
    buffer = BytesIO()
    Image.new("RGB", (20, 20), "purple").save(buffer, "PNG")
    source = save_image(buffer.getvalue(), images.directory)
    call = api.call

    async def with_attachments(method, **kwargs):
        result = await call(method, **kwargs)
        if method == "get_item":
            result["attachments"] = [{"id": "old-photo"}]
        return result

    api.call = with_attachments
    changes = []

    def update(**kwargs):
        changes.append(kwargs)
        return SimpleNamespace(id=kwargs["id"], attachments=[{"id": "new-photo"}])

    api.account.update_item = update
    store.enqueue(
        "images", "edit", "remote1", {"add_photos": [source], "replace_attachment_ids": ["old-photo"]}
    )
    await worker.once()
    assert changes[0]["remove_attachments"] == ["old-photo"]
    assert changes[0]["add_attachments"][0].startswith(b"\xff\xd8\xff")
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "SUCCEEDED"
    # A second confirmation based on stale IDs cannot remove a newly added server image.
    store.enqueue("stale-images", "edit", "remote1", {"add_photos": [source], "replace_attachment_ids": []})
    await worker.once()
    assert len(changes) == 1
    assert (
        store.rows("SELECT status FROM outbox WHERE operation_key='stale-images'")[0]["status"]
        == "PERMANENT_FAILED"
    )


@pytest.mark.parametrize(
    "changes", [{"price": "NaN"}, {"price": 0}, {"name": ""}, {"description": " "}, {"unknown": True}]
)
async def test_executor_validates_changes_at_server_boundary(setup, changes):
    store, api, _, worker = setup
    store.enqueue("bad-edit", "edit", "remote1", changes)
    await worker.once()
    assert not api.account.calls
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "PERMANENT_FAILED"


async def test_mark_read_is_durable_and_duplicate_confirmation_does_not_repeat(setup):
    store, api, _, worker = setup
    first = store.enqueue("read-once", "mark_read", "chat1", {})
    await worker.once()
    assert store.enqueue("read-once", "mark_read", "chat1", {}) == first
    assert not await worker.once()
    assert api.account.calls == ["markChatAsRead"]
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "SUCCEEDED"


@pytest.mark.parametrize(
    "response",
    [
        {"id": "wrong", "unread_messages_counter": 0},
        {"id": "chat1"},
        {"id": "chat1", "unread_messages_counter": 2},
    ],
)
async def test_mark_read_incomplete_response_is_uncertain_without_retry(setup, response):
    store, api, _, worker = setup
    api.account.mark_chat_as_read = lambda **kwargs: response
    store.enqueue("read-once", "mark_read", "chat1", {})
    await worker.once()
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"
    assert not await worker.once()
