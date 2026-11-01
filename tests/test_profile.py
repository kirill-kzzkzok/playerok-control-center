from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from app.playerok.adapter import plain
from app.rendering.cards import profile_card
from app.telegram.profile_panel import ProfilePanel, profile_avatar, profile_caption
from playerokapi.parser import account_profile


def viewer():
    # The real parser contract: balance/stats on profile, values may legitimately be zero.
    profile = account_profile(
        {
            "id": "seller",
            "profile": {
                "username": "<Seller>",
                "rating": 0,
                "testimonialCounter": 0,
                "createdAt": "2020-01-01T00:00:00+00:00",
            },
            "balance": {"value": 500, "available": 300, "withdrawable": 200, "frozen": 0},
            "stats": {
                "items": {"total": 10, "finished": 2},
                "deals": {"incoming": {"total": 3, "finished": 1}, "outgoing": {"total": 25, "finished": 20}},
            },
        }
    )
    return {"id": "seller", "username": "fallback", "profile": plain(profile)}


def test_profile_uses_parser_contract_and_separates_balances_and_directions():
    text = profile_caption(viewer())
    assert "&lt;Seller&gt;" in text
    assert "Рейтинг: 0 · Отзывов: 0" in text
    assert "Доступно: 300 ₽ · к выводу: 200 ₽" in text
    assert "Объявлений: 10 · завершено: 2" in text
    assert "Продаж: 25 · завершено: 20" in text
    assert "Покупок: 3 · завершено: 1" in text
    assert "01.01.2020 в 03:00" in text
    assert len(text) < 1024
    assert "нет данных" in profile_caption({})
    assert "None" not in profile_caption({"profile": {"rating": None}})


def test_invalid_avatar_retains_same_visible_initial_as_absent_avatar():
    profile = {"username": "Test", "rating": 4.8, "reviews_count": 12}
    assert profile_card(profile, b"invalid PNG") == profile_card(profile)
    source = BytesIO()
    Image.new("RGB", (30, 60), "green").save(source, "PNG")
    assert profile_card(profile, source.getvalue()) != profile_card(profile)


@pytest.mark.parametrize("mode", ["ok", "redirect", "oversize", "network"])
async def test_avatar_read_has_bounded_public_transport(monkeypatch, mode):
    original = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request)
        if mode == "network":
            raise httpx.ConnectError("mock", request=request)
        if mode == "redirect":
            return httpx.Response(302, headers={"location": "http://127.0.0.1"})
        return httpx.Response(200, content=b"x" * (5_000_001 if mode == "oversize" else 100))

    monkeypatch.setattr(
        "app.telegram.profile_panel.httpx.AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )
    assert await profile_avatar("https://untrusted.example/avatar.png") is None
    assert not requests
    result = await profile_avatar("https://playerok.com/avatar.png")
    assert result == (b"x" * 100 if mode == "ok" else None)
    assert len(requests) == 1
    assert "cookie" not in requests[0].headers
    assert "authorization" not in requests[0].headers


async def test_profile_panel_is_one_read_and_photo_without_avatar_network(monkeypatch):
    api = SimpleNamespace(call=AsyncMock(return_value=viewer()))
    avatar = AsyncMock(return_value=None)
    monkeypatch.setattr("app.telegram.profile_panel.profile_avatar", avatar)
    message = SimpleNamespace(answer_photo=AsyncMock())
    await ProfilePanel(api).handle(message)
    api.call.assert_awaited_once_with("get")
    image = message.answer_photo.call_args.args[0].data
    with Image.open(BytesIO(image)) as picture:
        assert picture.size == (1100, 480)
    assert "Продаж: 25" in message.answer_photo.call_args.kwargs["caption"]
