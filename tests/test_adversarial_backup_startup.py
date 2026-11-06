from tests.test_cold_telegram_recovery import (
    test_real_bootstrap_processes_new_paid_stock_before_cold_telegram_connection_recovers as bootstrap,
)
from tests.test_stage_a_controls import shop as shop_fixture

shop = shop_fixture


async def test_backup_directory_failure_does_not_kill_paid_stock_and_retries(shop, tmp_path, monkeypatch):
    from app.services import backup_retention

    original = backup_retention.startup_backup
    calls = []

    def backup(store, directory):
        calls.append(directory)
        if len(calls) == 1:
            raise PermissionError("injected unavailable backup directory")
        return original(store, directory)

    monkeypatch.setattr(backup_retention, "startup_backup", backup)
    await bootstrap(shop, tmp_path, monkeypatch, None)
    assert len(calls) >= 2
