"""Read-only Playerok auth/load check. Prints sanitized HTTP evidence, no account data."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from app.config import Config
from app.playerok.adapter import APIError, Playerok, SafeAccount


async def diagnose_playerok(config, account=None):
    account = account or SafeAccount(config.playerok_header, writes_enabled=False)
    api = Playerok(account)
    report = {"at": datetime.now(UTC).isoformat(), "mode": "READ_ONLY", "mutations": 0}
    try:
        viewer = await api.call("get")
        report["viewer"] = {
            "authorized": bool(viewer.get("id")),
            "profile_present": bool(viewer.get("profile")),
        }
        page = await api.call("get_transactions", count=1)
        report["transactions"] = {
            "page_present": isinstance(page.get("transactions"), list),
            "count": len(page.get("transactions") or []),
            "page_info_present": isinstance(page.get("page_info"), dict),
        }
        report["status"] = "OK"
    except APIError as error:
        report["status"], report["error_code"] = "ERROR", error.code
        if error.diagnostics:
            report["failure"] = error.diagnostics
    except Exception as error:  # noqa: BLE001 -- sanitized diagnostic boundary
        report["status"], report["error_type"] = "ERROR", type(error).__name__
    finally:
        report["http"] = list(account.response_diagnostics)
        account.close()
    return report


def main():
    report = asyncio.run(diagnose_playerok(Config.load()))
    path = Path("data/playerok_auth_diagnostics.json")
    path.parent.mkdir(exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
