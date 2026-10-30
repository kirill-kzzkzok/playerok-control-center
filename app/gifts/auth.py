"""One-time interactive login for the PERSONAL Telegram account (run locally)."""

import asyncio
from pathlib import Path

from app.gifts.settings import GiftSettings
from app.security.instance import InstanceLock


async def main():
    from telethon import TelegramClient

    s = GiftSettings.load()
    if not s.api_id or not s.api_hash:
        raise SystemExit("Set TG_USER_API_ID and TG_USER_API_HASH in private .env first")
    p = Path(s.session_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with InstanceLock(str(p) + ".lock"):
        async with TelegramClient(str(p), s.api_id, s.api_hash, request_retries=0) as client:
            me = await client.get_me()
            if getattr(me, "bot", False):
                raise SystemExit("Only personal Telegram user sessions are supported")
            print("Личный Telegram-аккаунт авторизован. Сессия сохранена локально; не выкладывайте её в Git.")


if __name__ == "__main__":
    asyncio.run(main())
