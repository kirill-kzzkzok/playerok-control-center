"""Read-only diagnostics; reports codes, never raw responses or exception strings."""

import json
from pathlib import Path

from app.config import Config
from app.playerok.adapter import APIError, Playerok, SafeAccount
from app.playerok.thread_calls import close_calls


async def diagnose(config):
    result = {"owner_allowlist_configured": bool(config.owner_ids), "writes_enabled": config.writes_enabled}
    from aiogram import Bot
    from aiogram.client.telegram import TelegramAPIServer

    from app.telegram.system_tunnel import happ_required
    from app.telegram.transport import TelegramNetworkSession

    mode = getattr(config, "telegram_transport", "legacy")
    worker = mode == "worker" or (mode == "legacy" and config.use_proxy)
    kwargs = {"api": TelegramAPIServer.from_base(config.telegram_proxy)} if worker else {}
    session = TelegramNetworkSession(timeout=15, system_tunnel=happ_required(mode), **kwargs)
    bot = Bot(config.telegram_token, session=session)
    try:
        await bot.get_me()
    except Exception as error:  # noqa: BLE001 -- diagnostics exports only a safe class name
        result["telegram_error"] = type(error).__name__
    finally:
        await session.close()
    result["telegram"] = session.results
    account = SafeAccount(config.playerok_header)
    api = Playerok(account)
    try:
        viewer = await api.call("get")
        result["playerok"] = {
            "authorized": bool(viewer.get("id")),
            "profile_present": bool(viewer.get("profile")),
        }
        for label, method, kwargs in [
            ("games", "get_games", {"count": 1}),
            ("items", "get_my_items", {"count": 1, "statuses": None}),
            ("orders", "get_deals", {"count": 1}),
            ("chats", "get_chats", {"count": 1}),
        ]:
            try:
                value = await api.call(method, **kwargs)
                result["playerok"][label] = {"read": isinstance(value, dict)}
            except APIError as error:
                result["playerok"][label] = {"error_code": error.code}
    except APIError as error:
        result["playerok"] = {"authorized": False, "error_code": error.code}
    finally:
        await close_calls(api)
        account.close()
    return result


async def main():
    config = Config.load()
    result = await diagnose(config)
    Path("data").mkdir(exist_ok=True)
    Path("data/live_diagnostics.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
