import asyncio
from decimal import Decimal, InvalidOperation

import httpx
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import BufferedInputFile

from app.playerok.adapter import safe_media_url
from app.rendering.cards import profile_card
from app.security.logging import event
from app.services.finance import checked_time
from app.services.playerok_connection import REASONS, PlayerokConnection
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
    def __init__(self, api, store=None):
        self.api, self.store = api, store

    def connection_caption(self, status):
        label = {
            "CONNECTED": "🟢 Подключён",
            "DISCONNECTED": "🔴 Не подключён",
            "UNKNOWN": "🟡 Не удалось проверить",
        }[status["status"]]
        lines = [
            "👤 <b>Профиль · Playerok</b>",
            label,
            "Аккаунт последней успешной проверки: " + escaped(status.get("username") or "не подтверждён", 80),
            "Успешная проверка: " + checked_time(status.get("last_success")),
            "Последняя попытка: " + checked_time(status.get("last_attempt")),
        ]
        if status.get("reason"):
            lines.append(REASONS.get(status["reason"], REASONS["CHECK_FAILED"]))
        if status.get("busy"):
            lines.append("Проверка уже выполняется либо прошло менее5 секунд. Новый запрос не отправлен.")
        return "\n".join(lines)

    async def handle(self, message, *, refresh=False, details=False):
        if self.store is not None:
            connection = getattr(self.api, "connection", None)
            initial_check = connection is None
            connection = connection or PlayerokConnection(self.store, self.api)
            status = await connection.check() if refresh or initial_check else dict(connection.state)
            text = self.connection_caption(status)
            withdrawal = self.store.setting(f"finance:{message.chat.id}:withdrawal", {})
            provider = withdrawal.get("provider") or {}
            text += "\n💳 Получение денег: " + escaped(
                provider.get("name") or provider.get("id") or "не выбрано", 60
            )
            markup = keyboard(
                [
                    [("🔄 Проверить соединение", "profile:check")],
                    [("Баланс и данные аккаунта", "profile:details")],
                    [("💳 Способ получения денег", "providers")],
                    [("✖️ Закрыть", "nav:close")],
                ],
                back=("⬅️ Настройки", "settings"),
            )
            if refresh:
                try:
                    return await message.edit_text(text, reply_markup=markup)
                except TelegramBadRequest as error:
                    if "message is not modified" in error.message.lower():
                        return None
                    if any(
                        v in error.message.lower()
                        for v in ("message to edit not found", "message can't be edited")
                    ):
                        return await message.answer(text, reply_markup=markup)
                    raise
            if details and status["status"] == "CONNECTED" and connection.viewer:
                try:
                    async with asyncio.timeout(8):
                        return await self.photo(message, connection.viewer, markup=markup)
                except (TelegramNetworkError, TimeoutError):
                    event("WARNING", "Картинка профиля: результат отправки неизвестен; повтора нет", level=30)
                    return None
            return await message.answer(text, reply_markup=markup)
        viewer = await self.api.call("get")
        return await self.photo(message, viewer)

    async def photo(self, message, viewer, *, markup=None):
        profile = dict(viewer.get("profile") or {})
        profile["username"] = profile.get("username") or viewer.get("username")
        avatar = await profile_avatar(profile.get("avatar_url"))
        image = await asyncio.to_thread(profile_card, profile, avatar)
        return await message.answer_photo(
            BufferedInputFile(image, filename="profile.png"),
            caption=profile_caption(viewer),
            reply_markup=markup or keyboard([[("✖️ Закрыть", "nav:close")]]),
        )
