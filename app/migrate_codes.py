"""Explicit owner-authorized identity migration; no polling or financial workers.

Run from the store directory: python -m app.migrate_codes
"""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.config import Config
from app.database.store import Store
from app.playerok.adapter import Playerok, SafeAccount
from app.playerok.thread_calls import close_calls
from app.security.instance import InstanceLock
from app.security.logging import configure_logging
from app.services.code_migration import CodeMigration
from app.services.playerok_connection import PlayerokConnection


async def migrate(config):
    if not config.writes_enabled:
        raise RuntimeError("PLAYEROK_WRITES_ENABLED выключен; миграция не начата")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    backup = config.database_path.parent / "backups" / ("before-title-codes-" + stamp + ".sqlite3")
    # Preserve the original schema too, before Store applies any upgrade.
    with sqlite3.connect(config.database_path.resolve().as_uri() + "?mode=ro", uri=True) as source:
        expected = source.execute("SELECT value FROM settings WHERE key='playerok:account_id'").fetchone()
        if not expected or not json.loads(expected[0]):
            raise RuntimeError("Привязка аккаунта отсутствует; миграция не начата")
        backup.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(backup) as destination:
            source.backup(destination)
    store = Store(config.database_path)
    account = SafeAccount(config.playerok_header, writes_enabled=config.writes_enabled)
    api = Playerok(account)
    try:
        connection = PlayerokConnection(store, api)
        result = await connection.check(cooldown=0)
        if result["status"] != "CONNECTED" or not account.writes_enabled:
            raise RuntimeError("Нужны подтверждённый аккаунт и PLAYEROK_WRITES_ENABLED=true")
        owners = store.rows(
            "SELECT DISTINCT owner_id FROM logical_products WHERE account_id=?", (account.id,)
        )
        reports = []
        for owner in owners:
            reports.append(await CodeMigration(store, api).run(owner["owner_id"], scan_catalog=True))
        report = {"at": stamp, "reports": reports, "verified": sum(r["verified"] for r in reports)}
        target = Path("data") / ("title-code-migration-" + stamp + ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "verified": report["verified"],
                    "problems": sum(len(r["problems"]) for r in reports),
                    "report": str(target),
                },
                ensure_ascii=False,
            )
        )
        return report
    finally:
        await close_calls(api)
        account.close()
        store.close()


def main():
    config = Config.load()
    configure_logging([config.telegram_token, config.playerok_header])
    with InstanceLock(str(config.database_path) + ".lock"):
        asyncio.run(migrate(config))


if __name__ == "__main__":
    main()
