import asyncio
from decimal import Decimal, InvalidOperation

import httpx
from aiogram.types import BufferedInputFile

from app.playerok.adapter import safe_media_url
from app.rendering.cards import profile_card
from app.services.finance import checked_time
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard


async def profile_avatar(url):
    if not url or not safe_media_url(url):
        return None
    try:
        async with (
            httpx.AsyncClient(timeout=10, follow_redirects=False) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                return None
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                if len(chunks) + len(chunk) > 5_000_000:
                    return None
                chunks.extend(chunk)
            return bytes(chunks)
    except httpx.HTTPError:
        return None


def profile_caption(viewer):
    profile = viewer.get("profile") or {}
    balance = profile.get("balance") or {}
    stats = profile.get("stats") or {}
    items = stats.get("items") or {}
    deals = stats.get("deals") or {}

    def shown(value):
        if value is None or isinstance(value, bool) or len(str(value)) > 20:
            return "нет данных"
        try:
            if not Decimal(str(value)).is_finite():
                return "нет данных"
        except InvalidOperation:
            return "нет данных"
        return escaped(value)

    lines = [
        "👤 <b>" + escaped(profile.get("username") or viewer.get("username") or "Без имени", 80) + "</b>",
        f"Рейтинг: {shown(profile.get('rating'))} · Отзывов: {shown(profile.get('reviews_count'))}",
        f"Баланс: {shown(balance.get('value'))} ₽",
        f"Доступно: {shown(balance.get('available'))} ₽ · к выводу: {shown(balance.get('withdrawable'))} ₽",
        f"Заморожено: {shown(balance.get('frozen'))} ₽",
        f"Объявлений: {shown(items.get('total'))} · завершено: {shown(items.get('finished'))}",
    ]
    for direction, label in [("outgoing", "Продаж"), ("incoming", "Покупок")]:
        values = deals.get(direction) or {}
        lines.append(f"{label}: {shown(values.get('total'))} · завершено: {shown(values.get('finished'))}")
    lines.extend(
        [
            "Регистрация: " + checked_time(profile.get("created_at") or viewer.get("created_at")),
            "Данные текущего ответа API. Баланс и счётчики не являются прибылью за период.",
        ]
    )
    return "\n".join(lines)


class ProfilePanel:
    def __init__(self, api):
        self.api = api

    async def handle(self, message):
        viewer = await self.api.call("get")
        profile = dict(viewer.get("profile") or {})
        profile["username"] = profile.get("username") or viewer.get("username")
        avatar = await profile_avatar(profile.get("avatar_url"))
        image = await asyncio.to_thread(profile_card, profile, avatar)
        return await message.answer_photo(
            BufferedInputFile(image, filename="profile.png"),
            caption=profile_caption(viewer),
            reply_markup=keyboard([]),
        )
