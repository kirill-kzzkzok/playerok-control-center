"""Read-only diagnostics; reports codes, never raw responses or exception strings."""

import json
from pathlib import Path

import httpx

from app.config import Config
from app.playerok.adapter import APIError, Playerok, SafeAccount


async def diagnose(config):
    result = {"owner_allowlist_configured": bool(config.owner_ids), "writes_enabled": config.writes_enabled}
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        for label, base in [("direct", "https://api.telegram.org"), ("worker", config.telegram_proxy)]:
            if not base:
                continue
            checks = {}
            for method in ["GET", "POST"]:
                try:
                    response = await client.request(method, base + "/bot" + config.telegram_token + "/getMe")
                    try:
                        body = response.json()
                    except ValueError:
                        body = {}
                    checks[method] = {
                        "http": response.status_code,
                        "telegram_ok": body.get("ok") is True,
                        "identity_present": bool((body.get("result") or {}).get("id")),
                    }
                except httpx.HTTPError as error:
                    checks[method] = {"error_type": type(error).__name__}
            try:
                response = await client.get(base + "/bot000000:invalid/getMe")
                checks["invalid_token"] = {
                    "http": response.status_code,
                    "correct_error": response.status_code in (401, 404),
                }
            except httpx.HTTPError as error:
                checks["invalid_token"] = {"error_type": type(error).__name__}
            result[label] = checks
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
