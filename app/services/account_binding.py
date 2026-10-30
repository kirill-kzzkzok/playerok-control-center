import json

from app.database.store import encode


class AccountBindingError(ValueError):
    pass


def bind_account(store, account_id):
    """One database belongs to one shop; changing credentials cannot adopt old actions."""
    if not isinstance(account_id, str) or not account_id.strip() or account_id != account_id.strip():
        raise AccountBindingError("Playerok не подтвердил идентификатор аккаунта")
    with store.transaction() as db:
        identities = set()
        for key in ("playerok:bound_account_id", "playerok:account_id", "transactions:account_id"):
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if row:
                try:
                    value = json.loads(row[0])
                except (TypeError, ValueError):
                    raise AccountBindingError("Повреждена привязка базы к аккаунту Playerok") from None
                if not isinstance(value, str) or not value.strip():
                    raise AccountBindingError("Повреждена привязка базы к аккаунту Playerok")
                identities.add(value)
        identities.update(
            row[0] for row in db.execute("SELECT DISTINCT account_id FROM account_transactions")
        )
        identities.update(
            row[0]
            for row in db.execute(
                "SELECT DISTINCT json_extract(payload,'$.seller.id') FROM orders "
                "WHERE json_extract(payload,'$.seller.id') IS NOT NULL"
            )
        )
        identities.update(
            row[0]
            for row in db.execute(
                "SELECT DISTINCT json_extract(payload,'$.item.user.id') FROM orders WHERE json_extract(payload,'$.item.user.id') IS NOT NULL"
            )
        )
        if identities and identities != {account_id}:
            raise AccountBindingError(
                "База привязана к другому аккаунту Playerok. Используйте отдельную базу для нового магазина; "
                "существующие данные и очередь не переназначены."
            )
        if not identities and (
            db.execute("SELECT 1 FROM orders LIMIT 1").fetchone()
            or db.execute("SELECT 1 FROM outbox LIMIT 1").fetchone()
        ):
            raise AccountBindingError(
                "Для существующих заказов или операций не установлена принадлежность аккаунту. "
                "Нужна проверка базы; автоматическая привязка запрещена."
            )
        for key in ("playerok:bound_account_id", "playerok:account_id"):
            db.execute(
                "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, encode(account_id)),
            )
