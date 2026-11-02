"""Upgrade actual linked SQLite rows; product UUID/config/history are immutable."""

import sqlite3
from pathlib import Path

import pytest

from app.database.store import Store, now


def old_store(path, monkeypatch):
    original_glob = Path.glob

    def old_glob(directory, pattern):
        for source in original_glob(directory, pattern):
            if directory.name == "migrations" and source.name.startswith("030_"):
                continue
            yield source

    with monkeypatch.context() as patch:
        patch.setattr(Path, "glob", old_glob)
        store = Store(path)
    return store


def test_identity_upgrade_preserves_uuid_children_and_flags(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    store = old_store(path, monkeypatch)
    store.claim_first_owner(7)
    stamp = now()
    store.execute(
        "INSERT INTO logical_products(id,code,owner_id,account_id,handler,config,state,stock_item_id,baseline,original_description,enabled,inherit_enabled,armed_at,created_at,updated_at) VALUES('stable-uuid','#2163148',7,'seller','fixed_text',?,'VERIFIED','old-item','{}','Description',1,1,?,?,?)",
        ('{"text":"secret"}', stamp, stamp, stamp),
    )
    store.execute(
        "INSERT INTO logical_listings(item_id,product_id,config_revision,description_sha256,server_status) VALUES('old-item','stable-uuid',1,'hash','APPROVED')"
    )
    before = store.rows("SELECT * FROM logical_products")[0]
    # Rebuild an actual pre-030 database from the pre-change table definition.
    # The fixture is never a working database.
    store.close()
    upgraded = Store(path)
    try:
        after = upgraded.rows("SELECT * FROM logical_products")[0]
        assert after == before
        assert upgraded.rows("SELECT product_id FROM logical_listings") == [{"product_id": "stable-uuid"}]
        assert not upgraded.rows("PRAGMA foreign_key_check")
        assert upgraded.rows("PRAGMA foreign_keys") == [{"foreign_keys": 1}]
        assert upgraded.rows(
            "SELECT old_code FROM logical_code_migrations WHERE product_id='stable-uuid'"
        ) == [{"old_code": "#2163148"}]
        upgraded.execute("UPDATE logical_products SET code='AB6' WHERE id='stable-uuid'")
        assert upgraded.rows("SELECT code FROM logical_products") == [{"code": "AB6"}]
    finally:
        upgraded.close()


def test_parent_rebuild_failure_rolls_back_and_restores_foreign_keys(tmp_path, monkeypatch):
    store = old_store(tmp_path / "rollback.sqlite", monkeypatch)
    store.claim_first_owner(7)
    original = Path.read_text

    def broken(path, *args, **kwargs):
        text = original(path, *args, **kwargs)
        if path.name == "030_logical_title_codes.sql":
            text = text.replace(
                "ALTER TABLE logical_products_next RENAME TO logical_products;",
                "SELECT missing FROM logical_products_next;\nALTER TABLE logical_products_next RENAME TO logical_products;",
            )
        return text

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", broken)
        with pytest.raises(sqlite3.OperationalError):
            store.apply_migrations()
    assert store.rows("PRAGMA foreign_keys") == [{"foreign_keys": 1}]
    assert not store.rows("SELECT 1 FROM schema_migrations WHERE version=30")
    assert store.rows("SELECT name FROM sqlite_master WHERE name='logical_products'")
    assert not store.rows("SELECT name FROM sqlite_master WHERE name='logical_products_next'")
    store.apply_migrations()
    assert store.rows("SELECT 1 FROM schema_migrations WHERE version=30")
    assert store.rows("PRAGMA integrity_check") == [{"integrity_check": "ok"}]
    assert not store.rows("PRAGMA foreign_key_check")
    store.close()
