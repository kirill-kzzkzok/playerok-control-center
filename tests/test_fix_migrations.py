"""Pre-025 SQLite backup upgrade with real service-created data; no live database."""

import json

import pytest

from app.database.store import Store
from app.services.logical_products import LogicalProducts
from app.services.orders import Orders
from tests.test_gifts import enroll
from tests.test_gifts import rig as gift_rig
from tests.test_logical_products import ReadAPI


@pytest.fixture(name="rig")
def isolated_rig(tmp_path):
    yield from gift_rig.__wrapped__(tmp_path)


async def test_prechange_backup_reopens_wal_preserves_gifts_stock_policies_and_logical_products(
    rig, tmp_path
):
    from app.services.account_binding import bind_account

    r = rig
    bind_account(r.store, "shop")
    await enroll(r)
    r.store.set_setting("gifts:stopped", True)
    r.store.set_setting("financial:test:payments_enabled", False)
    stock_order = {
        **r.api.orders["order"],
        "id": "stock-order",
        "item": {"id": "stock-lot", "user": {"id": "shop"}},
    }
    Orders(r.store).ingest(stock_order)
    Orders(r.store).add_stock("stock-lot", ["temporary-unit-only"])
    Orders(r.store).reserve("stock-order", actor=7)
    api = ReadAPI()
    api.account.id = "shop"
    api.item["user"]["id"] = "shop"
    logical = LogicalProducts(r.store, api)
    prepared = await logical.prepare(7, "lot", "fixed_text", {"text": "temporary-text-only"})
    logical.commit(7, prepared)
    tables = [
        "gift_catalog",
        "gift_listings",
        "gift_jobs",
        "gift_steps",
        "orders",
        "inventory",
        "reservations",
        "logical_products",
        "logical_listings",
        "delivery_policies",
        "settings",
    ]
    expected = {table: r.store.rows("SELECT * FROM " + table) for table in tables}
    operations = r.store.rows("SELECT id,operation_key,status,payload,attempts FROM outbox")
    for table, columns in {
        "outbox": ["started_at", "retry_at"],
        "operation_receipts": [
            "view_message_id",
            "view_token",
            "rendered_status",
            "send_retry_at",
            "send_error",
        ],
        "notifications": [
            "rendered_payload",
            "refresh_after",
            "refresh_failures",
            "send_retry_at",
            "send_error",
        ],
    }.items():
        for column in columns:
            r.store.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    r.store.execute("DROP INDEX logical_listing_order_snapshot")
    r.store.execute("ALTER TABLE logical_listings DROP COLUMN source_order_id")
    r.store.execute("ALTER TABLE logical_listings DROP COLUMN title_snapshot")
    r.store.execute("DROP TABLE chat_message_versions")
    r.store.execute("DELETE FROM schema_migrations WHERE version>=25")
    path = tmp_path / "prechange-copy.sqlite"
    r.store.backup(path)
    upgraded = Store(path)
    try:
        assert upgraded.rows("PRAGMA journal_mode") == [{"journal_mode": "wal"}]
        assert upgraded.rows("PRAGMA integrity_check") == [{"integrity_check": "ok"}]
        assert not upgraded.rows("PRAGMA foreign_key_check")
        for row in expected["logical_listings"]:
            parent = next(p for p in expected["logical_products"] if p["id"] == row["product_id"])
            row["title_snapshot"] = json.loads(parent["baseline"]).get("name")
        for parent in expected["logical_products"]:
            baseline = json.loads(parent["baseline"])
            if len(parent["code"]) == 3 and baseline.get("name"):
                baseline["name"] = parent["code"]
                from app.database.store import encode

                parent["baseline"] = encode(baseline)
        for table in tables:
            assert upgraded.rows("SELECT * FROM " + table) == expected[table], table
        assert upgraded.rows("SELECT id,operation_key,status,payload,attempts FROM outbox") == operations
        assert all(o["started_at"] is None for o in upgraded.rows("SELECT started_at FROM outbox"))
        assert upgraded.setting("gifts:stopped") is True
        assert not r.remote.payments
    finally:
        upgraded.close()
