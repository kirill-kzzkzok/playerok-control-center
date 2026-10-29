import asyncio
from io import BytesIO
from pathlib import Path

import httpx

from app.playerok.adapter import safe_media_url
from app.services.products import ValidationError, save_image

MAX_IMAGE_BYTES = 10_000_000


class LimitedBuffer(BytesIO):
    """Bound actual downloaded bytes even when Telegram metadata is absent/wrong."""

    def __init__(self, limit=MAX_IMAGE_BYTES):
        super().__init__()
        self.limit = limit

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise ValidationError("Изображение превышает 10 МБ")
        return super().write(data)


class Attachments:
    def __init__(self, directory="data/uploads"):
        self.directory = Path(directory).resolve()

    def path(self, source):
        path = Path(source).resolve()
        if not path.is_relative_to(self.directory) or path == self.directory:
            raise ValidationError("Небезопасный путь изображения")
        return path

    async def download(self, bot, file):
        if (file.file_size or 0) > MAX_IMAGE_BYTES:
            raise ValidationError("Изображение превышает 10 МБ")
        with LimitedBuffer() as stream:
            await bot.download(file.file_id, destination=stream)
            return await asyncio.to_thread(save_image, stream.getvalue(), self.directory)

    async def read(self, source):
        path = self.path(source)
        if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValidationError("Файл изображения отсутствует или превышает 10 МБ")
        return await asyncio.to_thread(path.read_bytes)

    async def remote(self, url):
        if not isinstance(url, str) or not safe_media_url(url):
            raise ValidationError("Недопустимый адрес изображения")
        try:
            async with (
                httpx.AsyncClient(timeout=15, follow_redirects=False) as client,
                client.stream("GET", url) as response,
            ):
                if response.status_code != 200:
                    raise ValidationError("Изображение недоступно на Playerok")
                with LimitedBuffer() as stream:
                    async for chunk in response.aiter_bytes():
                        stream.write(chunk)
                    source = await asyncio.to_thread(save_image, stream.getvalue(), self.directory)
        except httpx.HTTPError:
            raise ValidationError("Не удалось загрузить изображение с Playerok") from None
        try:
            return await self.read(source)
        finally:
            self.discard(source)

    def discard(self, source):
        # Only files produced in this upload root may ever be removed.
        self.path(source).unlink(missing_ok=True)
