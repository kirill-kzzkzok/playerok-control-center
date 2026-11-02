import socket
import sqlite3
from pathlib import Path
from unittest.mock import Mock

from app.config import Config
from app.database.store import Store
from app.selfcheck import selfcheck


def test_installation_smoke_uses_temporary_files_without_loading_credentials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = Mock(side_effect=AssertionError("Offline check must never read secrets"))
    monkeypatch.setattr(Config, "load", config)
    network = Mock(side_effect=AssertionError("Offline check must not connect to network"))
    monkeypatch.setattr(socket.socket, "connect", network)
    monkeypatch.setattr(socket, "create_connection", network)
    report = selfcheck()
    assert report["status"] == "OK", report
    assert report["external_requests"] == 0
    assert report["checks"]["sqlite_backup_reopened"]
    assert report["checks"]["profile_png"] and report["checks"]["chart_png"]
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / ".env").exists()
    config.assert_not_called()
    network.assert_not_called()


def test_installation_smoke_reports_missing_migration_without_secrets(monkeypatch):
    real_rows = Store.rows

    def rows(self, sql, args=()):
        if sql == "SELECT version FROM schema_migrations ORDER BY version":
            return []
        return real_rows(self, sql, args)

    monkeypatch.setattr(Store, "rows", rows)
    report = selfcheck()
    assert report["status"] == "ERROR"
    assert report["error_code"] == "MIGRATION_APPLICATION_MISMATCH"
    assert report["error_type"] == "ValueError"
    assert Path(report["package_path"]).name == "app"


def test_backup_closes_target_connection_instead_of_only_committing(tmp_path, monkeypatch):
    store = Store(tmp_path / "source.sqlite")
    original = sqlite3.connect
    targets = []

    def connect(*args, **kwargs):
        value = original(*args, **kwargs)
        targets.append(value)
        return value

    monkeypatch.setattr("app.database.store.sqlite3.connect", connect)
    try:
        backup = tmp_path / "backup.sqlite"
        store.backup(backup)
        assert len(targets) == 2  # Independent read-only source and backup target.
        import pytest

        for connection in targets:
            with pytest.raises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        backup.unlink()  # Also verifies Windows releases its handle immediately.
    finally:
        store.close()
