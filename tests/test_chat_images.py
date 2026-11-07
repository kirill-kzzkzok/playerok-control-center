import asyncio
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from PIL import Image

from app.database.store import Store
from app.playerok.adapter import APIError, SafeAccount
from app.services.attachments import Attachments
from app.services.products import ValidationError, save_image
from app.services.replies import Replies
from app.telegram.chat_panel import ChatPanel
from app.telegram.storage import SQLiteStorage
from app.workers.cleanup import UploadCleanup
from app.workers.outbox import Executor, OutboxWorker


def image_bytes():
    buffer = BytesIO()
    Image.new("RGB", (20, 20), "purple").save(buffer, "PNG")
    return buffer.getvalue()


class ImageAccount:
    writes_enabled = True

    def __init__(self):
        self.calls = []
        self.send_error = self.upload_error = None
        self.bad_upload = self.bad_images = False
        self.expired_upload = False

    @contextmanager
    def permit(self, operation):
        self.calls.append(operation)
        yield

    def upload_chat_image(self, image, chat_id, client_attachment_id):
        assert image.startswith(b"\xff\xd8\xff")
        if self.upload_error:
            raise self.upload_error
        return {
            "id": "temporary",
            "chatId": "wrong" if self.bad_upload else chat_id,
            "clientAttachmentId": client_attachment_id,
            "expiresAt": (datetime.now(UTC) + timedelta(hours=-1 if self.expired_upload else 1)).isoformat(),
        }

    def send_message_with_images(self, chat_id, text, image_ids):
        assert chat_id == "chat" and image_ids == ["temporary"]
        if self.send_error:
            raise self.send_error
        return {"id": "message", "images": [] if self.bad_images else [{"id": "file"}]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(tmp_path / "images.sqlite")
    attachments = Attachments(tmp_path / "uploads")
    monkeypatch.setattr("app.workers.outbox.Attachments", lambda: attachments)
    account = ImageAccount()
    api = SimpleNamespace(account=account, lock=asyncio.Lock(), call=AsyncMock(return_value={"id": "chat"}))
    worker = OutboxWorker(store, Executor(store, api, None))
    source = save_image(image_bytes(), attachments.directory)
    ident = Replies(store).enqueue(
        1, "photo", {"kind": "reply", "target": "chat", "payload": {"photos": [source], "text": "caption"}}
    )
    yield store, account, worker, source, ident, attachments
    store.close()


async def test_upload_then_send_is_journaled_and_cleanup_waits_for_success(setup):
    store, account, worker, _source, _ident, attachments = setup
    cleanup = UploadCleanup(store, attachments, grace_seconds=-1)
    assert cleanup.once()["removed"] == 0
    await worker.once()
    assert account.calls == ["uploadChatImageIntoTemporaryStore", "createChatMessage"]
    assert store.rows("SELECT status,attachment_id FROM chat_uploads") == [
        {"status": "UPLOADED", "attachment_id": "temporary"}
    ]
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "SUCCEEDED"
    assert json.loads(store.rows("SELECT result FROM outbox")[0]["result"])["image_count"] == 1
    assert not await worker.once()
    assert cleanup.once()["removed"] == 1


@pytest.mark.parametrize("stage", ["upload", "send", "wrong_chat", "missing_images", "expired_upload"])
async def test_photo_uncertainty_never_resends_and_preserves_local_file(setup, stage):
    store, account, worker, _source, _ident, attachments = setup
    if stage == "upload":
        account.upload_error = APIError("NETWORK_ERROR", uncertain=True)
    elif stage == "send":
        account.send_error = APIError("NETWORK_ERROR", uncertain=True)
    elif stage == "wrong_chat":
        account.bad_upload = True
    elif stage == "expired_upload":
        account.expired_upload = True
    else:
        account.bad_images = True
    await worker.once()
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "UNCERTAIN"
    count = len(account.calls)
    assert not await worker.once()
    assert len(account.calls) == count
    assert UploadCleanup(store, attachments, grace_seconds=-1).once()["removed"] == 0


def test_message_creation_passes_only_confirmed_attachment_ids_and_remains_guarded():
    requests = []

    def respond(request):
        body = json.loads(request.content)
        assert body["operationName"] == "createChatMessage"
        assert body["variables"] == {
            "input": {"chatId": "chat", "text": "caption", "imagesIds": ["temporary"]}
        }
        requests.append(request)
        return httpx.Response(
            200, json={"data": {"createChatMessage": {"id": "message", "images": [{"id": "file"}]}}}
        )

    account = SafeAccount(
        "token=private", writes_enabled=False, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    try:
        with pytest.raises(APIError, match="WRITES_DISABLED"), account.permit("createChatMessage"):
            account.send_message_with_images("chat", "caption", ["temporary"])
        assert not requests
        account.writes_enabled = True
        with account.permit("createChatMessage"):
            assert account.send_message_with_images("chat", "caption", ["temporary"])["id"] == "message"
        assert len(requests) == 1
    finally:
        account.close()


async def test_all_paths_validated_before_any_upload(setup):
    store, account, worker, source, _ident, attachments = setup
    store.execute(
        "UPDATE outbox SET payload=?",
        (json.dumps({"photos": [source, str(attachments.directory.parent / "outside.jpg")], "text": ""}),),
    )
    await worker.once()
    assert not account.calls
    assert store.rows("SELECT status FROM outbox")[0]["status"] == "PERMANENT_FAILED"


def test_exact_multipart_contract_requires_scoped_write_permission():
    requests = []

    def respond(request):
        requests.append(request)
        assert b"variables.file" in request.content
        assert b"clientAttachmentId" in request.content
        assert b"client-id" in request.content
        return httpx.Response(200, json={"data": {"uploadChatImageIntoTemporaryStore": {"id": "temporary"}}})

    account = SafeAccount(
        "token=private", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    try:
        with pytest.raises(APIError, match="WRITE_NOT_AUTHORIZED"):
            account.upload_chat_image(image_bytes(), "chat", "client-id")
        assert not requests
        with account.permit("uploadChatImageIntoTemporaryStore"):
            assert account.upload_chat_image(image_bytes(), "chat", "client-id") == {"id": "temporary"}
    finally:
        account.close()


async def test_remote_image_download_bounded_and_normalized_without_cookies(tmp_path, monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        assert "cookie" not in request.headers
        return httpx.Response(200, content=image_bytes())

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.attachments.httpx.AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    attachments = Attachments(tmp_path / "uploads")
    data = await attachments.remote("https://cdn.playerok.com/test.png")
    assert data.startswith(b"\xff\xd8\xff")
    assert not list(attachments.directory.glob("*.jpg"))
    for url in ["http://cdn.playerok.com/test.png", "https://evil.example/test.png"]:
        with pytest.raises(ValidationError, match="адрес"):
            await attachments.remote(url)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://evil.example/a"}),
        httpx.Response(200, content=b"not an image"),
        httpx.Response(200, content=b"x" * 10_000_001),
    ],
)
async def test_invalid_remote_content_does_not_create_local_file(tmp_path, monkeypatch, response):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.attachments.httpx.AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(lambda request: response), **kwargs),
    )
    attachments = Attachments(tmp_path / "uploads")
    with pytest.raises(ValidationError):
        await attachments.remote("https://cdn.playerok.com/image")
    assert not list(attachments.directory.glob("*.jpg"))


async def test_media_view_revalidates_message_and_rejects_deleted_and_negative_index(tmp_path):
    store = Store(tmp_path / "view.sqlite")
    state = FSMContext(SQLiteStorage(store), StorageKey(bot_id=1, chat_id=2, user_id=2))
    message = {"id": "message", "images": [{"id": "file", "url": "https://cdn.playerok.com/a.jpg"}]}
    page = {"messages": [message], "page_info": {"has_next_page": False}}
    store.set_setting("playerok:account_id", "seller")
    api = SimpleNamespace(account=SimpleNamespace(id="seller"), call=AsyncMock(return_value=page))
    panel = ChatPanel(store, api)
    panel.attachments.remote = AsyncMock(return_value=b"image")
    try:
        await panel.show("chat:chat", state)
        nonce = (await state.get_data())["chat_view"]["nonce"]
        data, _, _ = await panel.media(f"chatmedia:{nonce}:0", state)
        assert data == b"image"
        with pytest.raises(ValidationError):
            await panel.media(f"chatmedia:{nonce}:-1", state)
        message["deleted_at"] = "today"
        store.execute("UPDATE chat_messages SET payload=? WHERE id=?", (json.dumps(message), "message"))
        with pytest.raises(ValidationError, match="удалено"):
            await panel.media(f"chatmedia:{nonce}:0", state)
        assert panel.attachments.remote.await_count == 1
    finally:
        store.close()
